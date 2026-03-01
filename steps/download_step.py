"""
Download step — downloads wanted ZIPs from Receita Federal Nextcloud WebDAV.
Supports resume (.part files) and atomic rename on completion.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings
from logger import get_logger
from steps.base import StepResult

log = get_logger("step.download")


def _webdav_base() -> str:
    u = urlparse(settings.webdav_share_url)
    return f"{u.scheme}://{u.netloc}/public.php/webdav/"


def _webdav_auth_candidates(token: str) -> list[tuple[str, str]]:
    # Different Nextcloud setups accept either password=token or username=token.
    return [("", token), (token, "")]


def _is_wanted(name: str) -> bool:
    low = name.lower()
    return any(w.lower() in low for w in settings.wanted_files) and low.endswith(".zip")


def _download_file(client: httpx.Client, url: str, dest: Path) -> dict:
    """Download url to dest with resume support via .part file."""
    if dest.exists() and dest.stat().st_size > 0:
        log.info(f"already exists: {dest.name}")
        return {"name": dest.name, "status": "exists", "bytes": dest.stat().st_size}

    tmp = dest.with_suffix(dest.suffix + ".part")
    resume_pos = tmp.stat().st_size if tmp.exists() else 0

    headers = {}
    if resume_pos:
        headers["Range"] = f"bytes={resume_pos}-"
        log.info(f"resuming {dest.name} from {resume_pos / 1e6:.1f} MB")

    with client.stream("GET", url, headers=headers,
                       timeout=settings.download_timeout) as r:
        if r.status_code == 416:  # Range not satisfiable — restart
            resume_pos = 0
            tmp.unlink(missing_ok=True)
            # Re-request without Range header
            with client.stream("GET", url, timeout=settings.download_timeout) as r2:
                r2.raise_for_status()
                downloaded = 0
                with open(tmp, "wb") as f:
                    for chunk in r2.iter_bytes(chunk_size=settings.download_chunk_size):
                        f.write(chunk)
                        downloaded += len(chunk)
        else:
            r.raise_for_status()
            if resume_pos > 0 and r.status_code != 206:
                # Server ignored the Range header and returned the full file.
                # Appending would corrupt the ZIP — discard the partial file.
                log.warning(
                    f"{dest.name}: server returned {r.status_code} instead of 206, "
                    "restarting download from scratch"
                )
                tmp.unlink(missing_ok=True)
                resume_pos = 0
            mode = "ab" if resume_pos else "wb"
            downloaded = resume_pos
            with open(tmp, mode) as f:
                for chunk in r.iter_bytes(chunk_size=settings.download_chunk_size):
                    f.write(chunk)
                    downloaded += len(chunk)

    tmp.replace(dest)
    log.info(f"downloaded {dest.name} ({downloaded / 1e6:.1f} MB)")
    return {"name": dest.name, "status": "downloaded", "bytes": downloaded}


def _make_retry_download(client: httpx.Client, url: str, dest: Path) -> dict:
    @retry(
        stop=stop_after_attempt(settings.max_download_retries),
        wait=wait_exponential(multiplier=2, min=5, max=120),
        reraise=True,
    )
    def _inner() -> dict:
        return _download_file(client, url, dest)

    return _inner()


def run(job_id: str, run_key: str, checkpoint_dir: Path) -> StepResult:
    dest_dir = settings.data_dir / run_key
    dest_dir.mkdir(parents=True, exist_ok=True)

    base_url = _webdav_base()
    token = settings.webdav_token

    if not token:
        return StepResult.failed("WEBDAV_TOKEN is not configured")

    # Load file list from manifest saved by enqueue_job.py
    manifest_path = settings.checkpoint_dir / f"webdav_manifest_{run_key}.json"
    if not manifest_path.exists():
        return StepResult.failed(
            f"WebDAV manifest not found at {manifest_path} — run enqueue_job.py first"
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = [f for f in manifest.get("files", []) if _is_wanted(f["name"])]

    if not files:
        return StepResult.failed("no wanted ZIPs in manifest")

    results = []
    last_error: str | None = None
    for auth in _webdav_auth_candidates(token):
        results = []
        auth_failed = False
        with httpx.Client(auth=auth, follow_redirects=True) as client:
            for file_info in files:
                name = file_info["name"]
                rel_path = file_info.get("path") or name
                url = base_url + rel_path
                dest = dest_dir / name
                try:
                    res = _make_retry_download(client, url, dest)
                    results.append(res)
                except Exception as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status == 401:
                        auth_failed = True
                        last_error = str(exc)
                        log.warning(f"auth mode failed with 401 while downloading {name}; trying fallback auth")
                        break
                    log.error(f"failed to download {name} after retries: {exc}")
                    return StepResult.failed(f"download failed for {name}: {exc}")
        if not auth_failed:
            break

    if not results:
        return StepResult.failed(f"download authentication failed for all auth modes: {last_error}")

    # Write step artifact atomically
    artifact = checkpoint_dir / "download_manifest.json"
    tmp = artifact.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"run_key": run_key, "results": results}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(artifact)

    return StepResult.success(artifact_path=artifact, count=len(results))
