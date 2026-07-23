"""
Cleanup step — remove raw ZIPs, intermediate CSVs, and transformed CSVs, write ultimo_status.json.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from db.control import list_runs
from logger import get_logger
from steps.base import StepResult

log = get_logger("step.cleanup")


def _remove_glob(directory: Path, pattern: str) -> int:
    """Remove all files matching pattern in directory. Returns count removed."""
    removed = 0
    if not directory.exists():
        return 0
    for p in directory.glob(pattern):
        try:
            p.unlink()
            removed += 1
        except Exception as exc:
            log.warning(f"could not remove {p}: {exc}")
    return removed


def run(job_id: str, run_key: str, checkpoint_dir: Path) -> StepResult:
    data_dir = settings.data_dir / run_key
    removed = 0

    # Remove raw ZIPs
    removed += _remove_glob(data_dir, "*.zip")
    removed += _remove_glob(data_dir, "*.part")

    # Remove intermediate (pre-transform) CSVs
    csv_dir = data_dir / "csv"
    removed += _remove_glob(csv_dir, "*")
    if csv_dir.exists():
        try:
            csv_dir.rmdir()
        except OSError:
            pass

    # Remove transformed CSVs — already loaded into DB at this point
    transformed_dir = data_dir / "transformed"
    removed += _remove_glob(transformed_dir, "*")
    if transformed_dir.exists():
        try:
            transformed_dir.rmdir()
        except OSError:
            pass

    log.info(f"removed {removed} temp files from {data_dir}")

    # Write ultimo_status.json
    recent_runs = list_runs(limit=5)
    status = {
        "last_run_key": run_key,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "pipeline_status": "SUCCESS",
        "recent_runs": [dict(r) for r in recent_runs],
    }
    tmp = settings.status_file.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(status, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    tmp.replace(settings.status_file)
    log.info("ultimo_status.json updated")

    return StepResult.success(
        artifact_path=settings.status_file,
        removed_files=removed,
    )
