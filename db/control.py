"""SQLite control database — job queue and step tracking."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator

from config import settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> Path:
    return settings.control_db


def init_db() -> None:
    """Create tables if they don't exist."""
    schema = Path(__file__).parent / "control_schema.sql"
    with sqlite3.connect(_db_path()) as conn:
        conn.executescript(schema.read_text(encoding="utf-8"))
        conn.commit()


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Job queue ──────────────────────────────────────────────────────────────

def create_job(run_key: str, payload: dict | None = None) -> str:
    """Create a PENDING job for run_key. Returns job_id."""
    job_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO job_queue
                (job_id, run_key, status, attempts, max_attempts, created_at, payload_json)
            VALUES (?, ?, 'PENDING', 0, ?, ?, ?)
            """,
            (job_id, run_key, settings.max_job_attempts, _now(),
             json.dumps(payload or {}, ensure_ascii=False)),
        )
    return job_id


def requeue_job(run_key: str, payload: dict | None = None) -> str:
    """
    Reset an existing job for run_key back to PENDING so it can be re-processed.
    Clears attempts, lease, error and all step records.
    Returns the existing job_id.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT job_id FROM job_queue WHERE run_key = ?", (run_key,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no job found for run_key={run_key}")
        job_id = row["job_id"]
        conn.execute(
            """
            UPDATE job_queue
            SET status = 'PENDING', attempts = 0, lease_until = NULL,
                started_at = NULL, finished_at = NULL, last_error = NULL,
                payload_json = ?
            WHERE job_id = ?
            """,
            (json.dumps(payload or {}, ensure_ascii=False), job_id),
        )
        conn.execute("DELETE FROM job_steps WHERE job_id = ?", (job_id,))
    return job_id


def get_job_by_run_key(run_key: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM job_queue WHERE run_key = ?", (run_key,)
        ).fetchone()


def acquire_job(lease_seconds: int = 3600) -> sqlite3.Row | None:
    """
    Atomically acquire the next available job using BEGIN IMMEDIATE to prevent
    race conditions when multiple workers are running.
    Returns the acquired job row (pre-update snapshot) or None.
    """
    lease_until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
    now = _now()

    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        # BEGIN IMMEDIATE acquires a write lock before SELECT,
        # preventing two workers from selecting the same job concurrently.
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            """
            SELECT * FROM job_queue
            WHERE status IN ('PENDING', 'FAILED')
              AND attempts < max_attempts
              AND (lease_until IS NULL OR lease_until < ?)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (now,),
        ).fetchone()

        if row is None:
            conn.rollback()
            return None

        conn.execute(
            """
            UPDATE job_queue
            SET status = 'RUNNING',
                attempts = attempts + 1,
                started_at = ?,
                lease_until = ?
            WHERE job_id = ?
            """,
            (now, lease_until, row["job_id"]),
        )
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def renew_lease(job_id: str, lease_seconds: int = 3600) -> None:
    lease_until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE job_queue SET lease_until = ? WHERE job_id = ? AND status = 'RUNNING'",
            (lease_until, job_id),
        )


def finish_job(job_id: str, success: bool, error: str | None = None) -> None:
    status = "SUCCESS" if success else "FAILED"
    with get_conn() as conn:
        if not success:
            row = conn.execute(
                "SELECT attempts, max_attempts FROM job_queue WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row and row["attempts"] >= row["max_attempts"]:
                status = "DEAD"

        conn.execute(
            """
            UPDATE job_queue
            SET status = ?, finished_at = ?, last_error = ?
            WHERE job_id = ?
            """,
            (status, _now(), error, job_id),
        )


def list_runs(limit: int = 20) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM job_queue ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()


# ── Step tracking ───────────────────────────────────────────────────────────

def upsert_step(job_id: str, step_name: str, status: str,
                error: str | None = None, artifact_path: str | None = None) -> None:
    now = _now()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM job_steps WHERE job_id = ? AND step_name = ?",
            (job_id, step_name),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE job_steps
                SET status = ?,
                    attempts = attempts + 1,
                    finished_at = CASE WHEN ? IN ('SUCCESS','FAILED','SKIPPED') THEN ? ELSE finished_at END,
                    started_at  = CASE WHEN ? = 'RUNNING' THEN ? ELSE started_at END,
                    error = ?,
                    artifact_path = ?
                WHERE job_id = ? AND step_name = ?
                """,
                (status, status, now, status, now, error, artifact_path, job_id, step_name),
            )
        else:
            conn.execute(
                """
                INSERT INTO job_steps
                    (job_id, step_name, status, attempts, started_at, finished_at, error, artifact_path)
                VALUES (?, ?, ?, 1,
                    CASE WHEN ? = 'RUNNING' THEN ? ELSE NULL END,
                    CASE WHEN ? IN ('SUCCESS','FAILED','SKIPPED') THEN ? ELSE NULL END,
                    ?, ?)
                """,
                (job_id, step_name, status, status, now, status, now, error, artifact_path),
            )


def get_step(job_id: str, step_name: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM job_steps WHERE job_id = ? AND step_name = ?",
            (job_id, step_name),
        ).fetchone()


def get_steps(job_id: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM job_steps WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
