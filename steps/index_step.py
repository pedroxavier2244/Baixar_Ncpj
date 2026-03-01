"""
Index step — refresh the mv_cnpj_full materialized view and validate row count.
Uses REFRESH MATERIALIZED VIEW CONCURRENTLY (non-blocking for readers).
First-ever refresh must use non-CONCURRENT since view starts WITH NO DATA.
"""
from __future__ import annotations

import json
from pathlib import Path

import psycopg

from config import settings
from logger import get_logger
from steps.base import StepResult

log = get_logger("step.index")


def _view_has_rows(conn: psycopg.Connection) -> bool:
    """Check if mv_cnpj_full already has data (needed to choose refresh mode)."""
    row = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM cnpj.mv_cnpj_full LIMIT 1)"
    ).fetchone()
    return bool(row and row[0])


def run(job_id: str, run_key: str, checkpoint_dir: Path) -> StepResult:
    try:
        with psycopg.connect(settings.postgres_url, autocommit=True) as conn:
            has_data = _view_has_rows(conn)

            if has_data:
                log.info("refreshing mv_cnpj_full CONCURRENTLY (non-blocking)...")
                conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY cnpj.mv_cnpj_full")
            else:
                log.info("first-ever refresh of mv_cnpj_full (non-concurrent)...")
                conn.execute("REFRESH MATERIALIZED VIEW cnpj.mv_cnpj_full")

            log.info("refresh complete — counting rows...")
            row = conn.execute("SELECT COUNT(*) FROM cnpj.mv_cnpj_full").fetchone()
            count = row[0] if row else 0
            log.info(f"mv_cnpj_full has {count:,} rows")

            if count == 0:
                return StepResult.failed(
                    "mv_cnpj_full is empty after refresh — check load step output"
                )

    except Exception as exc:
        import traceback
        return StepResult.failed(f"index step failed: {exc}\n{traceback.format_exc()}")

    artifact = checkpoint_dir / "index_manifest.json"
    tmp = artifact.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"run_key": run_key, "mv_rows": count}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(artifact)

    return StepResult.success(artifact_path=artifact, mv_rows=count)
