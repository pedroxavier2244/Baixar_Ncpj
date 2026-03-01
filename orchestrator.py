"""
Orchestrator — runs steps in order with checkpoint/resume support.
Each step is skipped if it already has a SUCCESS record in SQLite.
"""
from __future__ import annotations

import traceback
from pathlib import Path
from typing import Callable

from db.control import get_step, upsert_step
from logger import get_logger
from steps.base import StepResult, StepStatus

log = get_logger("orchestrator")

StepFn = Callable[[str, str, Path], StepResult]  # (job_id, run_key, checkpoint_dir) -> StepResult

STEPS: list[tuple[str, StepFn]] = []  # populated by register_steps()


def register_steps() -> None:
    """Import step modules and build STEPS list. Safe to call multiple times."""
    global STEPS
    if STEPS:
        return
    from steps.download_step import run as download
    from steps.verify_step import run as verify
    from steps.extract_step import run as extract
    from steps.transform_step import run as transform
    from steps.load_step import run as load
    from steps.index_step import run as index
    from steps.cleanup_step import run as cleanup

    STEPS = [
        ("download", download),
        ("verify", verify),
        ("extract", extract),
        ("transform", transform),
        ("load", load),
        ("index", index),
        ("cleanup", cleanup),
    ]


def run_pipeline(job_id: str, run_key: str, checkpoint_dir: Path,
                 force: bool = False) -> bool:
    """
    Execute all steps in order. Skips steps already SUCCESS (unless force=True).
    Returns True if all steps succeeded.
    """
    register_steps()
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    for step_name, step_fn in STEPS:
        existing = get_step(job_id, step_name)
        if existing and existing["status"] == "SUCCESS" and not force:
            log.info(f"[{step_name}] already SUCCESS — skipping")
            continue

        log.info(f"[{step_name}] starting")
        upsert_step(job_id, step_name, "RUNNING")

        try:
            result: StepResult = step_fn(job_id, run_key, checkpoint_dir)
        except Exception as exc:
            tb = traceback.format_exc()
            log.error(f"[{step_name}] unhandled exception: {exc}\n{tb}")
            result = StepResult.failed(error=str(exc) + "\n" + tb)

        artifact = str(result.artifact_path) if result.artifact_path else None

        if result.status == StepStatus.SUCCESS:
            log.info(f"[{step_name}] SUCCESS artifact={artifact}")
            upsert_step(job_id, step_name, "SUCCESS", artifact_path=artifact)
        elif result.status == StepStatus.SKIPPED:
            log.info(f"[{step_name}] SKIPPED reason={result.metadata.get('reason')}")
            upsert_step(job_id, step_name, "SKIPPED")
        else:
            log.error(f"[{step_name}] FAILED error={result.error}")
            upsert_step(job_id, step_name, "FAILED", error=result.error)
            return False  # halt pipeline on step failure

    return True
