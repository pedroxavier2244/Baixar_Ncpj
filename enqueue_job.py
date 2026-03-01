"""
Check if a new month is published on Receita Federal Nextcloud WebDAV.
If yes (or --force), create a PENDING job in SQLite.

Usage:
    python enqueue_job.py                    # auto-detect latest month
    python enqueue_job.py --run-key 2026-02  # specific month
    python enqueue_job.py --force            # re-enqueue even if already done
    python enqueue_job.py --check-only       # just print status, no enqueue
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings
from db.control import create_job, get_job_by_run_key, init_db, requeue_job
from logger import get_logger

log = get_logger("enqueue_job")

PROPFIND_BODY = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:displayname/>
    <d:getcontentlength/>
    <d:getlastmodified/>
    <d:getetag/>
  </d:prop>
</d:propfind>"""


def _webdav_base(share_url: str) -> str:
    u = urlparse(share_url)
    return f"{u.scheme}://{u.netloc}/public.php/webdav/"


def _webdav_auth_candidates(token: str) -> list[tuple[str, str]]:
    # Different Nextcloud setups accept either password=token or username=token.
    return [("", token), (token, "")]


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
def propfind_listing(token: str, rel_path: str = "") -> list[dict]:
    """Return listing items from WebDAV PROPFIND for rel_path."""
    base = _webdav_base(settings.webdav_share_url)
    url = base + rel_path.lstrip("/")
    last_error: Exception | None = None
    r: httpx.Response | None = None
    for auth in _webdav_auth_candidates(token):
        with httpx.Client(auth=auth, timeout=60) as client:
            resp = client.request(
                "PROPFIND", url,
                headers={"Depth": "1", "Content-Type": "application/xml"},
                content=PROPFIND_BODY.encode(),
            )
        if resp.status_code == 401:
            last_error = httpx.HTTPStatusError(
                "unauthorized with attempted auth mode",
                request=resp.request,
                response=resp,
            )
            continue
        resp.raise_for_status()
        r = resp
        break

    if r is None:
        if last_error is not None:
            raise last_error
        raise RuntimeError("WebDAV PROPFIND failed without response")

    root = ET.fromstring(r.text)
    ns = {"d": "DAV:"}
    items = []
    prefix = "/public.php/webdav/"
    for response in root.findall("d:response", ns):
        href = (response.findtext("d:href", "", ns) or "")
        if prefix not in href:
            continue
        rel = href.split(prefix, 1)[1].lstrip("/")
        if not rel:
            continue
        is_dir = rel.endswith("/")
        rel_clean = rel.rstrip("/")
        name = unquote(rel_clean.split("/")[-1])
        props = response.find("d:propstat/d:prop", ns)
        if props is None:
            continue
        items.append({
            "name": name,
            "path": rel_clean,
            "is_dir": is_dir,
            "size": int(props.findtext("d:getcontentlength", "0", ns) or 0),
            "modified": props.findtext("d:getlastmodified", "", ns),
            "etag": (props.findtext("d:getetag", "", ns) or "").strip('"'),
        })
    return items


def _detect_run_key(items: list[dict]) -> str:
    """Infer run_key (YYYY-MM) from filenames or fall back to current month."""
    month_re = re.compile(r"(\d{4})[-_]?(\d{2})", re.IGNORECASE)
    months: set[str] = set()
    for item in items:
        m = month_re.search(item["name"])
        if m:
            months.add(f"{m.group(1)}-{m.group(2)}")
    return sorted(months)[-1] if months else datetime.now(timezone.utc).strftime("%Y-%m")


def _manifest_path(run_key: str) -> Path:
    return settings.checkpoint_dir / f"webdav_manifest_{run_key}.json"


def _load_manifest(run_key: str) -> dict | None:
    p = _manifest_path(run_key)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _save_manifest(run_key: str, items: list[dict]) -> None:
    settings.ensure_dirs()
    p = _manifest_path(run_key)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"run_key": run_key, "files": items}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(p)


def _files_changed(old: dict | None, new_items: list[dict]) -> bool:
    """True if the new listing differs from the saved manifest (by etag)."""
    if old is None:
        return True
    old_map = {f["name"]: f["etag"] for f in old.get("files", [])}
    new_map = {f["name"]: f["etag"] for f in new_items}
    return old_map != new_map


def _filter_wanted(items: list[dict]) -> list[dict]:
    return [
        i for i in items
        if not i.get("is_dir")
        and i["name"].lower().endswith(".zip")
        and any(w.lower() in i["name"].lower() for w in settings.wanted_files)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Enqueue CNPJ ETL job")
    parser.add_argument("--run-key", help="YYYY-MM to process (default: auto-detect)")
    parser.add_argument("--force", action="store_true",
                        help="Re-enqueue even if already SUCCESS")
    parser.add_argument("--check-only", action="store_true",
                        help="Only check status, do not create job")
    args = parser.parse_args()

    init_db()
    settings.ensure_dirs()

    if not settings.webdav_token:
        log.error("WEBDAV_TOKEN is not set in .env â€” cannot proceed")
        sys.exit(1)

    log.info("checking Receita Federal WebDAV for new files...")
    try:
        items = propfind_listing(settings.webdav_token)
    except Exception as exc:
        log.error(f"WebDAV listing failed: {exc}")
        sys.exit(1)

    run_key = args.run_key or _detect_run_key(items)
    wanted = _filter_wanted(items)
    if not wanted:
        try:
            month_items = propfind_listing(settings.webdav_token, rel_path=f"{run_key}/")
            wanted = _filter_wanted(month_items)
        except Exception as exc:
            log.error(f"WebDAV month listing failed for {run_key}: {exc}")
            sys.exit(1)

    if not wanted:
        log.error("no wanted ZIPs found in WebDAV listing - check token/URL and wanted_files config")
        sys.exit(1)

    log.info(f"detected run_key={run_key}, files={len(wanted)}")

    if args.check_only:
        print(json.dumps({"run_key": run_key, "files": wanted}, indent=2, ensure_ascii=False))
        sys.exit(0)

    # Check if already processed
    existing = get_job_by_run_key(run_key)
    if existing and existing["status"] == "SUCCESS" and not args.force:
        log.info(f"run_key={run_key} already SUCCESS â€” nothing to do (use --force to override)")
        sys.exit(0)

    # Check if files changed since last manifest
    old_manifest = _load_manifest(run_key)
    if not _files_changed(old_manifest, wanted) and not args.force:
        log.info(f"files unchanged since last run of {run_key} â€” skipping enqueue")
        sys.exit(0)

    # Save manifest and create/reset job
    _save_manifest(run_key, wanted)
    payload = {"files": wanted, "enqueued_at": datetime.now(timezone.utc).isoformat()}
    if existing:
        job_id = requeue_job(run_key, payload=payload)
        log.info(f"job reset to PENDING job_id={job_id} run_key={run_key}")
    else:
        job_id = create_job(run_key, payload=payload)
        log.info(f"job created job_id={job_id} run_key={run_key}")
    print(f"OK job_id={job_id} run_key={run_key}")


if __name__ == "__main__":
    main()

