"""
Verify step — validate downloaded ZIPs:
  - file exists and size > 0
  - valid ZIP structure (no corruption)
  - SHA256 checksum computed and recorded
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from config import settings
from logger import get_logger
from steps.base import StepResult

log = get_logger("step.verify")


def _sha256(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while data := f.read(chunk):
            h.update(data)
    return h.hexdigest()


def _verify_zip(path: Path) -> str | None:
    """Return error string or None if the ZIP is valid."""
    if not path.exists():
        return f"file not found: {path}"
    if path.stat().st_size == 0:
        return f"empty file: {path}"
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad:
                return f"corrupt member in ZIP: {bad}"
    except zipfile.BadZipFile as exc:
        return f"bad ZIP file: {exc}"
    return None


def run(job_id: str, run_key: str, checkpoint_dir: Path) -> StepResult:
    # Load the download manifest to know which files to verify
    download_artifact = checkpoint_dir / "download_manifest.json"
    if not download_artifact.exists():
        return StepResult.failed(
            "download_manifest.json not found — run download step first"
        )

    manifest = json.loads(download_artifact.read_text(encoding="utf-8"))
    dest_dir = settings.data_dir / run_key

    verification: list[dict] = []
    errors: list[str] = []

    for item in manifest.get("results", []):
        name = item["name"]
        path = dest_dir / name

        err = _verify_zip(path)
        sha = _sha256(path) if err is None else None

        entry = {"name": name, "ok": err is None, "sha256": sha, "error": err}
        verification.append(entry)

        if err:
            log.error(f"verify FAIL {name}: {err}")
            errors.append(f"{name}: {err}")
        else:
            log.info(f"verify OK {name} sha256={sha[:12]}...")  # type: ignore[index]

    # Write artifact atomically
    artifact = checkpoint_dir / "verify_manifest.json"
    tmp = artifact.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"run_key": run_key, "files": verification}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(artifact)

    if errors:
        return StepResult.failed(f"verification failed for {len(errors)} file(s): {'; '.join(errors)}")

    return StepResult.success(artifact_path=artifact, verified=len(verification))
