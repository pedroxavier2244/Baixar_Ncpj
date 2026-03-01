# ETL CNPJ Receita Federal — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a robust, professional ETL that downloads CNPJ data from Receita Federal (Nextcloud WebDAV), loads it into PostgreSQL, and exposes a FastAPI with support for 100+ concurrent users — running automatically on Windows Server via Task Scheduler.

**Architecture:** SQLite job queue with lease/heartbeat controls a worker that executes 7 sequential steps with per-step checkpoints. Each step writes artifacts atomically. On crash/restart the orchestrator resumes from the last successful step. FastAPI runs under Gunicorn with async workers and psycopg3 connection pool.

**Tech Stack:** Python 3.11+, psycopg3 (async), FastAPI, Gunicorn+Uvicorn, SQLite, tenacity, pydantic-settings, httpx, python-dotenv

---

## Task 1: Project Foundation — config, requirements, .env

**Files:**
- Create: `config.py`
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `.gitignore`

**Step 1: Create `.gitignore`**

```
.env
.venv/
__pycache__/
*.pyc
*.pyo
pipeline_control.db
data/
checkpoints/
logs/
ultimo_status.json
```

**Step 2: Create `requirements.txt`**

```
# Core
pydantic-settings>=2.2
python-dotenv>=1.0

# HTTP / download
httpx>=0.27
tenacity>=8.3
beautifulsoup4>=4.12

# Database
psycopg[binary,pool]>=3.1
aiosqlite>=0.20

# ETL / data
pyarrow>=15.0

# API
fastapi>=0.111
uvicorn[standard]>=0.29
gunicorn>=22.0

# Utils
rich>=13.7
```

**Step 3: Create `config.py`**

```python
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # PostgreSQL
    postgres_url: str = "postgresql://user:pass@localhost:5432/cnpj_db"

    # Paths
    base_dir: Path = Path(__file__).parent
    data_dir: Path = base_dir / "data"
    log_dir: Path = base_dir / "logs"
    checkpoint_dir: Path = base_dir / "checkpoints"
    control_db: Path = base_dir / "pipeline_control.db"
    status_file: Path = base_dir / "ultimo_status.json"

    # Download source
    webdav_share_url: str = "https://arquivos.receitafederal.gov.br/index.php/s/YggdBLfdninEJX9"
    webdav_token: str = "YggdBLfdninEJX9"
    download_timeout: int = 300
    download_chunk_size: int = 1024 * 1024  # 1 MB
    max_download_retries: int = 5

    # Pipeline
    max_job_attempts: int = 3
    lease_seconds: int = 3600          # 1 hour max per job
    heartbeat_interval: int = 30       # renew lease every 30s

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_workers: int = 8
    db_pool_min: int = 5
    db_pool_max: int = 20

    # Postgres schema
    pg_schema: str = "cnpj"

    # Files to download (keywords in filename)
    wanted_files: list[str] = [
        "Empresas", "Estabelecimentos", "Socios",
        "Cnaes", "Municipios", "Naturezas",
        "Qualificacoes", "Motivos", "Paises", "Portes",
    ]

    def ensure_dirs(self) -> None:
        for d in [self.data_dir, self.log_dir, self.checkpoint_dir]:
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
```

**Step 4: Create `.env.example`**

```env
# PostgreSQL da empresa
POSTGRES_URL=postgresql://user:senha@host:5432/cnpj_db

# Paths (opcional — padrão: subpastas do projeto)
DATA_DIR=C:/etl_cnpj/data
LOG_DIR=C:/etl_cnpj/logs
CHECKPOINT_DIR=C:/etl_cnpj/checkpoints

# Download
WEBDAV_SHARE_URL=https://arquivos.receitafederal.gov.br/index.php/s/YggdBLfdninEJX9
WEBDAV_TOKEN=YggdBLfdninEJX9
MAX_DOWNLOAD_RETRIES=5

# Pipeline
MAX_JOB_ATTEMPTS=3
LEASE_SECONDS=3600
HEARTBEAT_INTERVAL=30

# API
API_HOST=0.0.0.0
API_PORT=8000
API_WORKERS=8
DB_POOL_MIN=5
DB_POOL_MAX=20
```

**Step 5: Commit**

```bash
git add config.py requirements.txt .env.example .gitignore
git commit -m "feat: project foundation — config, requirements, env"
```

---

## Task 2: SQLite Control Database

**Files:**
- Create: `db/__init__.py`
- Create: `db/control_schema.sql`
- Create: `db/control.py`

**Step 1: Create `db/control_schema.sql`**

```sql
CREATE TABLE IF NOT EXISTS job_queue (
    job_id        TEXT PRIMARY KEY,
    run_key       TEXT NOT NULL UNIQUE,   -- ex: "2026-02"
    status        TEXT NOT NULL DEFAULT 'PENDING',
                  -- PENDING | RUNNING | SUCCESS | FAILED | DEAD
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    lease_until   TEXT,                   -- ISO timestamp
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    last_error    TEXT,
    payload_json  TEXT                    -- JSON com metadados do run
);

CREATE TABLE IF NOT EXISTS job_steps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT NOT NULL REFERENCES job_queue(job_id),
    step_name     TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'PENDING',
                  -- PENDING | RUNNING | SUCCESS | FAILED | SKIPPED
    attempts      INTEGER NOT NULL DEFAULT 0,
    started_at    TEXT,
    finished_at   TEXT,
    error         TEXT,
    artifact_path TEXT,                   -- caminho do checkpoint
    UNIQUE(job_id, step_name)
);

CREATE INDEX IF NOT EXISTS idx_job_queue_status ON job_queue(status);
CREATE INDEX IF NOT EXISTS idx_job_steps_job ON job_steps(job_id);
```

**Step 2: Create `db/control.py`**

```python
"""SQLite control database — job queue and step tracking."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
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


def get_job_by_run_key(run_key: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM job_queue WHERE run_key = ?", (run_key,)
        ).fetchone()


def acquire_job(lease_seconds: int = 3600) -> sqlite3.Row | None:
    """
    Atomically acquire the next available job (PENDING or FAILED with attempts
    remaining and expired/no lease). Returns the row or None.
    """
    from datetime import timedelta
    lease_until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
    now = _now()

    with get_conn() as conn:
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
            return None

        conn.execute(
            """
            UPDATE job_queue
            SET status = 'RUNNING', attempts = attempts + 1,
                started_at = ?, lease_until = ?
            WHERE job_id = ?
            """,
            (now, lease_until, row["job_id"]),
        )
    return row


def renew_lease(job_id: str, lease_seconds: int = 3600) -> None:
    from datetime import timedelta
    lease_until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE job_queue SET lease_until = ? WHERE job_id = ?",
            (lease_until, job_id),
        )


def finish_job(job_id: str, success: bool, error: str | None = None) -> None:
    status = "SUCCESS" if success else "FAILED"
    with get_conn() as conn:
        # If this was the last attempt and still failed → DEAD
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
                SET status = ?, attempts = attempts + 1,
                    finished_at = CASE WHEN ? IN ('SUCCESS','FAILED','SKIPPED')
                                       THEN ? ELSE finished_at END,
                    started_at  = CASE WHEN ? = 'RUNNING' THEN ? ELSE started_at END,
                    error = ?, artifact_path = ?
                WHERE job_id = ? AND step_name = ?
                """,
                (status, status, now, status, now, error, artifact_path, job_id, step_name),
            )
        else:
            conn.execute(
                """
                INSERT INTO job_steps
                    (job_id, step_name, status, attempts, started_at, finished_at, error, artifact_path)
                VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (job_id, step_name, status,
                 now if status == "RUNNING" else None,
                 now if status in ("SUCCESS", "FAILED", "SKIPPED") else None,
                 error, artifact_path),
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
```

**Step 3: Create `db/__init__.py`** (empty)

**Step 4: Commit**

```bash
git add db/
git commit -m "feat: SQLite control database — job_queue and job_steps"
```

---

## Task 3: Logging Setup

**Files:**
- Create: `logger.py`

**Step 1: Create `logger.py`**

```python
"""Structured JSON logging with daily rotation."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from config import settings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Extra fields (run_id, step, etc.)
        for key in ("run_id", "run_key", "step", "job_id"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload, ensure_ascii=False)


def get_logger(name: str, run_date: str | None = None) -> logging.Logger:
    """Return a logger that writes JSON to file and plain text to stdout."""
    settings.ensure_dirs()

    log_date = run_date or datetime.utcnow().strftime("%Y-%m-%d")
    log_file = settings.log_dir / f"etl_{log_date}.log"

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured

    logger.setLevel(logging.DEBUG)

    # File handler (JSON, daily rotation, keep 30 days)
    fh = TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=30, encoding="utf-8"
    )
    fh.setFormatter(JsonFormatter())
    fh.setLevel(logging.DEBUG)
    logger.addHandler(fh)

    # Stdout handler (human-readable)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    sh.setLevel(logging.INFO)
    logger.addHandler(sh)

    return logger
```

**Step 2: Commit**

```bash
git add logger.py
git commit -m "feat: structured JSON logging with daily rotation"
```

---

## Task 4: Base Step + Orchestrator

**Files:**
- Create: `steps/__init__.py`
- Create: `steps/base.py`
- Create: `orchestrator.py`

**Step 1: Create `steps/base.py`**

```python
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class StepStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass
class StepResult:
    status: StepStatus
    artifact_path: Path | None = None
    metadata: dict = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def success(cls, artifact_path: Path | None = None, **meta) -> "StepResult":
        return cls(status=StepStatus.SUCCESS, artifact_path=artifact_path, metadata=meta)

    @classmethod
    def skipped(cls, reason: str = "") -> "StepResult":
        return cls(status=StepStatus.SKIPPED, metadata={"reason": reason})

    @classmethod
    def failed(cls, error: str) -> "StepResult":
        return cls(status=StepStatus.FAILED, error=error)
```

**Step 2: Create `orchestrator.py`**

```python
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

STEPS: list[tuple[str, StepFn]] = []  # populated after step modules are imported


def register_steps() -> None:
    """Import step modules and build STEPS list."""
    from steps.download_step import run as download
    from steps.verify_step import run as verify
    from steps.extract_step import run as extract
    from steps.transform_step import run as transform
    from steps.load_step import run as load
    from steps.index_step import run as index
    from steps.cleanup_step import run as cleanup

    global STEPS
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
        _log = log  # could bind run_id / step here with LoggerAdapter

        # Check existing state
        existing = get_step(job_id, step_name)
        if existing and existing["status"] == "SUCCESS" and not force:
            _log.info(f"[{step_name}] already SUCCESS — skipping")
            upsert_step(job_id, step_name, "SKIPPED",
                        artifact_path=existing["artifact_path"])
            continue

        _log.info(f"[{step_name}] starting")
        upsert_step(job_id, step_name, "RUNNING")

        try:
            result: StepResult = step_fn(job_id, run_key, checkpoint_dir)
        except Exception as exc:
            tb = traceback.format_exc()
            _log.error(f"[{step_name}] unhandled exception: {exc}\n{tb}")
            result = StepResult.failed(error=str(exc) + "\n" + tb)

        artifact = str(result.artifact_path) if result.artifact_path else None

        if result.status == StepStatus.SUCCESS:
            _log.info(f"[{step_name}] SUCCESS artifact={artifact}")
            upsert_step(job_id, step_name, "SUCCESS", artifact_path=artifact)
        elif result.status == StepStatus.SKIPPED:
            _log.info(f"[{step_name}] SKIPPED reason={result.metadata.get('reason')}")
            upsert_step(job_id, step_name, "SKIPPED")
        else:
            _log.error(f"[{step_name}] FAILED error={result.error}")
            upsert_step(job_id, step_name, "FAILED", error=result.error)
            return False  # halt pipeline on step failure

    return True
```

**Step 3: Commit**

```bash
git add steps/ orchestrator.py
git commit -m "feat: base step dataclass and pipeline orchestrator with checkpoint/resume"
```

---

## Task 5: Worker (lease + heartbeat)

**Files:**
- Create: `worker.py`

**Step 1: Create `worker.py`**

```python
"""
Worker — polls job queue, acquires lease, runs pipeline with heartbeat.

Usage:
    python worker.py            # loop forever
    python worker.py --once     # run one job and exit
"""
from __future__ import annotations

import argparse
import sys
import threading
import time

from config import settings
from db.control import acquire_job, finish_job, init_db, renew_lease
from logger import get_logger
from orchestrator import run_pipeline

log = get_logger("worker")


def _heartbeat(job_id: str, stop_event: threading.Event) -> None:
    """Renew lease every heartbeat_interval seconds until stop_event is set."""
    while not stop_event.wait(timeout=settings.heartbeat_interval):
        try:
            renew_lease(job_id, settings.lease_seconds)
        except Exception as exc:
            log.warning(f"heartbeat renew failed: {exc}")


def process_one_job() -> bool:
    """Acquire and process a single job. Returns True if a job was processed."""
    job = acquire_job(settings.lease_seconds)
    if job is None:
        return False

    job_id = job["job_id"]
    run_key = job["run_key"]
    log.info(f"acquired job job_id={job_id} run_key={run_key} attempt={job['attempts']}")

    checkpoint_dir = settings.checkpoint_dir / job_id

    # Start heartbeat thread
    stop_event = threading.Event()
    hb = threading.Thread(target=_heartbeat, args=(job_id, stop_event), daemon=True)
    hb.start()

    success = False
    error: str | None = None
    try:
        success = run_pipeline(job_id, run_key, checkpoint_dir)
    except Exception as exc:
        import traceback
        error = traceback.format_exc()
        log.error(f"pipeline crashed: {exc}")
    finally:
        stop_event.set()
        hb.join(timeout=5)

    finish_job(job_id, success=success, error=error)
    status = "SUCCESS" if success else "FAILED/DEAD"
    log.info(f"job {job_id} finished with {status}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="ETL CNPJ Worker")
    parser.add_argument("--once", action="store_true",
                        help="Process one job then exit")
    parser.add_argument("--poll-interval", type=int, default=60,
                        help="Seconds between queue polls (default: 60)")
    args = parser.parse_args()

    init_db()
    log.info("worker started")

    if args.once:
        processed = process_one_job()
        if not processed:
            log.info("no jobs available — exiting")
        sys.exit(0)

    # Continuous loop
    while True:
        try:
            processed = process_one_job()
            if not processed:
                log.debug(f"no jobs — sleeping {args.poll_interval}s")
                time.sleep(args.poll_interval)
        except KeyboardInterrupt:
            log.info("worker interrupted — shutting down")
            break
        except Exception as exc:
            log.error(f"unexpected worker error: {exc}")
            time.sleep(30)


if __name__ == "__main__":
    main()
```

**Step 2: Commit**

```bash
git add worker.py
git commit -m "feat: worker with lease, heartbeat, and pipeline execution"
```

---

## Task 6: Enqueue Job (WebDAV detection + job creation)

**Files:**
- Create: `enqueue_job.py`

**Step 1: Create `enqueue_job.py`**

```python
"""
Check if a new month is published on Receita Federal Nextcloud.
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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings
from db.control import create_job, get_job_by_run_key, init_db
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


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
def propfind_listing(token: str) -> list[dict]:
    """Return list of {name, size, modified, etag} from WebDAV PROPFIND."""
    base = _webdav_base(settings.webdav_share_url)
    with httpx.Client(auth=("", token), timeout=60) as client:
        r = client.request(
            "PROPFIND", base,
            headers={"Depth": "1", "Content-Type": "application/xml"},
            content=PROPFIND_BODY,
        )
        r.raise_for_status()

    # Parse XML minimally (avoid lxml dependency)
    import xml.etree.ElementTree as ET
    root = ET.fromstring(r.text)
    ns = {"d": "DAV:"}
    items = []
    for response in root.findall("d:response", ns):
        href = (response.findtext("d:href", "", ns) or "").rstrip("/")
        name = href.split("/")[-1]
        if not name or not name.lower().endswith(".zip"):
            continue
        props = response.find("d:propstat/d:prop", ns)
        if props is None:
            continue
        items.append({
            "name": name,
            "size": int(props.findtext("d:getcontentlength", "0", ns) or 0),
            "modified": props.findtext("d:getlastmodified", "", ns),
            "etag": props.findtext("d:getetag", "", ns),
        })
    return items


def _detect_run_key(items: list[dict]) -> str:
    """
    Infer run_key (YYYY-MM) from the latest modified ZIP.
    Falls back to current year-month if none detected.
    """
    # Try to extract YYYY-MM from filename pattern
    month_re = re.compile(r"(\d{4})[-_]?(\d{2})", re.IGNORECASE)
    months = set()
    for item in items:
        m = month_re.search(item["name"])
        if m:
            months.add(f"{m.group(1)}-{m.group(2)}")
    if months:
        return sorted(months)[-1]
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _manifest_path(run_key: str) -> Path:
    return settings.checkpoint_dir / f"webdav_manifest_{run_key}.json"


def _load_manifest(run_key: str) -> dict | None:
    p = _manifest_path(run_key)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def _save_manifest(run_key: str, items: list[dict]) -> None:
    settings.ensure_dirs()
    p = _manifest_path(run_key)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"run_key": run_key, "files": items}, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(p)


def _files_changed(old: dict | None, new_items: list[dict]) -> bool:
    if old is None:
        return True
    old_map = {f["name"]: f["etag"] for f in old.get("files", [])}
    new_map = {f["name"]: f["etag"] for f in new_items}
    return old_map != new_map


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

    log.info("checking Receita Federal WebDAV for new files...")
    items = propfind_listing(settings.webdav_token)

    if not items:
        log.error("no ZIPs found in WebDAV listing — check token/URL")
        sys.exit(1)

    # Filter to wanted files only
    wanted = [i for i in items
              if any(w.lower() in i["name"].lower() for w in settings.wanted_files)]

    run_key = args.run_key or _detect_run_key(wanted)
    log.info(f"detected run_key={run_key}, files={len(wanted)}")

    if args.check_only:
        print(json.dumps({"run_key": run_key, "files": wanted}, indent=2, ensure_ascii=False))
        sys.exit(0)

    # Check if already processed
    existing = get_job_by_run_key(run_key)
    if existing and existing["status"] == "SUCCESS" and not args.force:
        log.info(f"run_key={run_key} already SUCCESS — nothing to do (use --force to override)")
        sys.exit(0)

    # Check if files changed since last manifest
    old_manifest = _load_manifest(run_key)
    if not _files_changed(old_manifest, wanted) and not args.force:
        log.info(f"files unchanged since last run of {run_key} — skipping enqueue")
        sys.exit(0)

    # Save manifest and create job
    _save_manifest(run_key, wanted)
    job_id = create_job(run_key, payload={"files": wanted, "enqueued_at": datetime.now(timezone.utc).isoformat()})
    log.info(f"job created job_id={job_id} run_key={run_key}")
    print(f"OK job_id={job_id} run_key={run_key}")


if __name__ == "__main__":
    main()
```

**Step 2: Commit**

```bash
git add enqueue_job.py
git commit -m "feat: enqueue_job with WebDAV PROPFIND detection and manifest diff"
```

---

## Task 7: Download Step

**Files:**
- Create: `steps/download_step.py`

**Step 1: Create `steps/download_step.py`**

```python
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


def _is_wanted(name: str) -> bool:
    low = name.lower()
    return any(w.lower() in low for w in settings.wanted_files) and low.endswith(".zip")


def _get_remote_size(client: httpx.Client, url: str) -> int | None:
    try:
        r = client.head(url, follow_redirects=True, timeout=30)
        cl = r.headers.get("content-length")
        return int(cl) if cl and cl.isdigit() else None
    except Exception:
        return None


def _download_file(client: httpx.Client, url: str, dest: Path) -> dict:
    if dest.exists() and dest.stat().st_size > 0:
        log.info(f"already exists: {dest.name}")
        return {"name": dest.name, "status": "exists", "bytes": dest.stat().st_size}

    tmp = dest.with_suffix(dest.suffix + ".part")
    resume_pos = tmp.stat().st_size if tmp.exists() else 0

    headers = {}
    if resume_pos:
        headers["Range"] = f"bytes={resume_pos}-"
        log.info(f"resuming {dest.name} from {resume_pos/1e6:.1f} MB")

    with client.stream("GET", url, headers=headers, timeout=settings.download_timeout) as r:
        if r.status_code == 416:  # Range not satisfiable — restart
            resume_pos = 0
            tmp.unlink(missing_ok=True)
            r = client.stream("GET", url, timeout=settings.download_timeout).__enter__()
        r.raise_for_status()

        mode = "ab" if resume_pos else "wb"
        downloaded = resume_pos
        with open(tmp, mode) as f:
            for chunk in r.iter_bytes(chunk_size=settings.download_chunk_size):
                f.write(chunk)
                downloaded += len(chunk)

    tmp.replace(dest)
    log.info(f"downloaded {dest.name} ({downloaded/1e6:.1f} MB)")
    return {"name": dest.name, "status": "downloaded", "bytes": downloaded}


@retry(stop=stop_after_attempt(settings.max_download_retries),
       wait=wait_exponential(multiplier=2, min=5, max=120),
       reraise=True)
def _download_with_retry(client: httpx.Client, url: str, dest: Path) -> dict:
    return _download_file(client, url, dest)


def run(job_id: str, run_key: str, checkpoint_dir: Path) -> StepResult:
    dest_dir = settings.data_dir / run_key
    dest_dir.mkdir(parents=True, exist_ok=True)

    base_url = _webdav_base()
    token = settings.webdav_token

    # Load file list from manifest
    manifest_path = settings.checkpoint_dir / f"webdav_manifest_{run_key}.json"
    if not manifest_path.exists():
        return StepResult.failed("WebDAV manifest not found — run enqueue_job.py first")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = [f for f in manifest.get("files", []) if _is_wanted(f["name"])]

    if not files:
        return StepResult.failed("no wanted ZIPs in manifest")

    results = []
    with httpx.Client(auth=("", token), follow_redirects=True) as client:
        for file_info in files:
            name = file_info["name"]
            url = base_url + name
            dest = dest_dir / name
            try:
                res = _download_with_retry(client, url, dest)
                results.append(res)
            except Exception as exc:
                log.error(f"failed to download {name}: {exc}")
                return StepResult.failed(f"download failed for {name}: {exc}")

    # Write step artifact
    artifact = checkpoint_dir / "download_manifest.json"
    tmp = artifact.with_suffix(".tmp")
    tmp.write_text(json.dumps({"run_key": run_key, "results": results}, indent=2), encoding="utf-8")
    tmp.replace(artifact)

    return StepResult.success(artifact_path=artifact, count=len(results))
```

**Step 2: Commit**

```bash
git add steps/download_step.py
git commit -m "feat: download step with WebDAV, retry, and resume"
```

---

## Task 8: Verify Step

**Files:**
- Create: `steps/verify_step.py`

**Step 1: Create `steps/verify_step.py`**

```python
"""
Verify step — validate downloaded ZIPs: size > 0, valid ZIP structure.
Optionally checks SHA256 if manifest contains expected hash.
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
    """Return error string or None if valid."""
    if not path.exists():
        return f"file not found: {path}"
    if path.stat().st_size == 0:
        return f"empty file: {path}"
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad:
                return f"corrupt member in ZIP: {bad}"
    except zipfile.BadZipFile as e:
        return f"bad ZIP: {e}"
    return None


def run(job_id: str, run_key: str, checkpoint_dir: Path) -> StepResult:
    # Load download manifest
    download_artifact = checkpoint_dir / "download_manifest.json"
    if not download_artifact.exists():
        return StepResult.failed("download_manifest.json not found — run download step first")

    manifest = json.loads(download_artifact.read_text(encoding="utf-8"))
    dest_dir = settings.data_dir / run_key

    verification = []
    errors = []

    for item in manifest.get("results", []):
        name = item["name"]
        path = dest_dir / name

        err = _verify_zip(path)
        sha = _sha256(path) if not err else None

        entry = {"name": name, "ok": err is None, "sha256": sha, "error": err}
        verification.append(entry)

        if err:
            log.error(f"verify FAIL {name}: {err}")
            errors.append(f"{name}: {err}")
        else:
            log.info(f"verify OK {name} sha256={sha[:12]}...")

    artifact = checkpoint_dir / "verify_manifest.json"
    tmp = artifact.with_suffix(".tmp")
    tmp.write_text(json.dumps({"run_key": run_key, "files": verification}, indent=2), encoding="utf-8")
    tmp.replace(artifact)

    if errors:
        return StepResult.failed(f"verification failed: {'; '.join(errors)}")

    return StepResult.success(artifact_path=artifact, verified=len(verification))
```

**Step 2: Commit**

```bash
git add steps/verify_step.py
git commit -m "feat: verify step — ZIP integrity and SHA256"
```

---

## Task 9: Extract Step

**Files:**
- Create: `steps/extract_step.py`

**Step 1: Create `steps/extract_step.py`**

```python
"""
Extract step — unzip downloaded ZIPs into organized CSV files.
Uses streaming extraction (no loading entire ZIP into RAM).
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from config import settings
from logger import get_logger
from steps.base import StepResult

log = get_logger("step.extract")

CHUNK = 8 * 1024 * 1024  # 8 MB streaming buffer


def _extract_zip(zip_path: Path, dest_dir: Path) -> list[str]:
    """Extract all members of zip_path into dest_dir. Returns list of extracted names."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    extracted = []
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            out_path = dest_dir / Path(member.filename).name  # flatten path
            if out_path.exists() and out_path.stat().st_size > 0:
                log.info(f"  already extracted: {out_path.name}")
                extracted.append(out_path.name)
                continue
            tmp = out_path.with_suffix(out_path.suffix + ".tmp")
            with zf.open(member) as src, open(tmp, "wb") as dst:
                while chunk := src.read(CHUNK):
                    dst.write(chunk)
            tmp.replace(out_path)
            log.info(f"  extracted: {out_path.name} ({out_path.stat().st_size/1e6:.1f} MB)")
            extracted.append(out_path.name)
    return extracted


def run(job_id: str, run_key: str, checkpoint_dir: Path) -> StepResult:
    download_artifact = checkpoint_dir / "download_manifest.json"
    if not download_artifact.exists():
        return StepResult.failed("download_manifest.json missing")

    manifest = json.loads(download_artifact.read_text(encoding="utf-8"))
    zip_dir = settings.data_dir / run_key
    csv_dir = settings.data_dir / run_key / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)

    all_extracted = []
    for item in manifest.get("results", []):
        zip_path = zip_dir / item["name"]
        if not zip_path.exists():
            return StepResult.failed(f"ZIP not found: {zip_path}")
        log.info(f"extracting {zip_path.name}...")
        names = _extract_zip(zip_path, csv_dir)
        all_extracted.extend(names)

    artifact = checkpoint_dir / "extract_manifest.json"
    tmp = artifact.with_suffix(".tmp")
    tmp.write_text(json.dumps({"run_key": run_key, "csv_dir": str(csv_dir),
                               "files": all_extracted}, indent=2), encoding="utf-8")
    tmp.replace(artifact)

    return StepResult.success(artifact_path=artifact, extracted=len(all_extracted))
```

**Step 2: Commit**

```bash
git add steps/extract_step.py
git commit -m "feat: extract step — streaming ZIP extraction"
```

---

## Task 10: Transform Step

**Files:**
- Create: `steps/transform_step.py`

**Step 1: Create `steps/transform_step.py`**

```python
"""
Transform step — normalize Receita Federal CSV files (encoding, columns, types).
Processes in streaming chunks. Outputs clean CSVs ready for COPY into Postgres.
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from config import settings
from logger import get_logger
from steps.base import StepResult

log = get_logger("step.transform")

# Receita Federal uses latin-1 and ';' separator
RF_ENCODING = "latin-1"
RF_SEP = ";"
CHUNK_ROWS = 50_000


# Schema definitions: (output_name, rf_column_index_or_name, type)
# Receita Federal files have NO headers — column positions are fixed.
# Reference: layout docs from RFB.

TABLE_SCHEMAS: dict[str, dict] = {
    "empresas": {
        "columns": [
            "cnpj_basico", "razao_social", "natureza_juridica",
            "qualificacao_responsavel", "capital_social", "porte",
            "ente_federativo_responsavel",
        ],
        "filename_keyword": "Empresas",
    },
    "estabelecimentos": {
        "columns": [
            "cnpj_basico", "cnpj_ordem", "cnpj_dv", "identificador_matriz_filial",
            "nome_fantasia", "situacao_cadastral", "data_situacao_cadastral",
            "motivo_situacao_cadastral", "nm_cidade_exterior", "pais",
            "data_inicio_atividade", "cnae_fiscal", "cnae_fiscal_secundaria",
            "tipo_logradouro", "logradouro", "numero", "complemento",
            "bairro", "cep", "uf", "municipio", "ddd1", "telefone1",
            "ddd2", "telefone2", "ddd_fax", "fax",
            "correio_eletronico", "situacao_especial", "data_situacao_especial",
        ],
        "filename_keyword": "Estabelecimentos",
    },
    "socios": {
        "columns": [
            "cnpj_basico", "identificador_socio", "nome_socio",
            "cnpj_cpf_socio", "qualificacao_socio",
            "data_entrada_sociedade", "pais", "representante_legal",
            "nome_representante", "qualificacao_representante", "faixa_etaria",
        ],
        "filename_keyword": "Socios",
    },
    "cnaes": {
        "columns": ["codigo", "descricao"],
        "filename_keyword": "Cnaes",
    },
    "municipios": {
        "columns": ["codigo", "descricao"],
        "filename_keyword": "Municipios",
    },
    "naturezas": {
        "columns": ["codigo", "descricao"],
        "filename_keyword": "Naturezas",
    },
    "qualificacoes": {
        "columns": ["codigo", "descricao"],
        "filename_keyword": "Qualificacoes",
    },
    "motivos": {
        "columns": ["codigo", "descricao"],
        "filename_keyword": "Motivos",
    },
    "paises": {
        "columns": ["codigo", "descricao"],
        "filename_keyword": "Paises",
    },
    "portes": {
        "columns": ["codigo", "descricao"],
        "filename_keyword": "Portes",
    },
}


def _clean(value: str) -> str:
    """Strip whitespace and normalize null-like values."""
    v = value.strip()
    return "" if v in ("00000000", "0001-01-01", "N/A", "NA") else v


def _find_csv_for_table(csv_dir: Path, keyword: str) -> list[Path]:
    return sorted([
        p for p in csv_dir.iterdir()
        if keyword.lower() in p.name.lower() and p.suffix.upper() in (".CSV", "")
    ])


def _transform_table(csv_dir: Path, schema: dict, out_dir: Path) -> dict:
    keyword = schema["filename_keyword"]
    columns = schema["columns"]
    sources = _find_csv_for_table(csv_dir, keyword)

    if not sources:
        log.warning(f"no CSV found for keyword '{keyword}'")
        return {"keyword": keyword, "rows": 0, "files": []}

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{keyword.lower()}.csv"
    tmp_path = out_path.with_suffix(".tmp")

    total = 0
    with open(tmp_path, "w", encoding="utf-8", newline="") as out_f:
        writer = csv.writer(out_f, delimiter=",", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(columns)  # header

        for src in sources:
            log.info(f"  transforming {src.name}...")
            with open(src, "r", encoding=RF_ENCODING, errors="replace", newline="") as in_f:
                reader = csv.reader(in_f, delimiter=RF_SEP)
                for row in reader:
                    # Pad or trim to expected column count
                    padded = (row + [""] * len(columns))[: len(columns)]
                    cleaned = [_clean(v) for v in padded]
                    writer.writerow(cleaned)
                    total += 1

    tmp_path.replace(out_path)
    log.info(f"  {keyword}: {total:,} rows → {out_path.name}")
    return {"keyword": keyword, "rows": total, "out": str(out_path)}


def run(job_id: str, run_key: str, checkpoint_dir: Path) -> StepResult:
    extract_artifact = checkpoint_dir / "extract_manifest.json"
    if not extract_artifact.exists():
        return StepResult.failed("extract_manifest.json missing")

    manifest = json.loads(extract_artifact.read_text(encoding="utf-8"))
    csv_dir = Path(manifest["csv_dir"])
    out_dir = settings.data_dir / run_key / "transformed"

    results = []
    for table_name, schema in TABLE_SCHEMAS.items():
        result = _transform_table(csv_dir, schema, out_dir)
        results.append(result)

    artifact = checkpoint_dir / "transform_manifest.json"
    tmp = artifact.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "run_key": run_key,
        "out_dir": str(out_dir),
        "tables": results,
    }, indent=2), encoding="utf-8")
    tmp.replace(artifact)

    total_rows = sum(r["rows"] for r in results)
    log.info(f"transform complete: {total_rows:,} total rows")
    return StepResult.success(artifact_path=artifact, total_rows=total_rows)
```

**Step 2: Commit**

```bash
git add steps/transform_step.py
git commit -m "feat: transform step — RF CSV normalization in streaming chunks"
```

---

## Task 11: PostgreSQL Schema

**Files:**
- Create: `db/schema.sql`

**Step 1: Create `db/schema.sql`**

```sql
-- ============================================================
-- ETL CNPJ — PostgreSQL Schema
-- Run once: psql $POSTGRES_URL -f db/schema.sql
-- ============================================================

CREATE SCHEMA IF NOT EXISTS cnpj;

-- Enable trigram extension for full-text search
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ── Source tables ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS cnpj.rf_empresas (
    cnpj_basico                  CHAR(8)        NOT NULL,
    razao_social                 TEXT,
    natureza_juridica            CHAR(4),
    qualificacao_responsavel     CHAR(2),
    capital_social               TEXT,
    porte                        CHAR(2),
    ente_federativo_responsavel  TEXT,
    run_key                      CHAR(7),        -- YYYY-MM
    PRIMARY KEY (cnpj_basico)
);

CREATE TABLE IF NOT EXISTS cnpj.rf_estabelecimentos (
    cnpj_basico                  CHAR(8)        NOT NULL,
    cnpj_ordem                   CHAR(4)        NOT NULL,
    cnpj_dv                      CHAR(2)        NOT NULL,
    identificador_matriz_filial  CHAR(1),
    nome_fantasia                TEXT,
    situacao_cadastral           CHAR(2),
    data_situacao_cadastral      CHAR(8),
    motivo_situacao_cadastral    CHAR(2),
    nm_cidade_exterior           TEXT,
    pais                         CHAR(3),
    data_inicio_atividade        CHAR(8),
    cnae_fiscal                  CHAR(7),
    cnae_fiscal_secundaria       TEXT,
    tipo_logradouro              TEXT,
    logradouro                   TEXT,
    numero                       TEXT,
    complemento                  TEXT,
    bairro                       TEXT,
    cep                          CHAR(8),
    uf                           CHAR(2),
    municipio                    CHAR(7),
    ddd1                         TEXT,
    telefone1                    TEXT,
    ddd2                         TEXT,
    telefone2                    TEXT,
    ddd_fax                      TEXT,
    fax                          TEXT,
    correio_eletronico           TEXT,
    situacao_especial            TEXT,
    data_situacao_especial       CHAR(8),
    run_key                      CHAR(7),
    PRIMARY KEY (cnpj_basico, cnpj_ordem, cnpj_dv)
);

CREATE TABLE IF NOT EXISTS cnpj.rf_socios (
    id                           BIGSERIAL      PRIMARY KEY,
    cnpj_basico                  CHAR(8),
    identificador_socio          CHAR(1),
    nome_socio                   TEXT,
    cnpj_cpf_socio               TEXT,
    qualificacao_socio           CHAR(2),
    data_entrada_sociedade       CHAR(8),
    pais                         CHAR(3),
    representante_legal          TEXT,
    nome_representante           TEXT,
    qualificacao_representante   CHAR(2),
    faixa_etaria                 CHAR(1),
    run_key                      CHAR(7)
);

CREATE TABLE IF NOT EXISTS cnpj.rf_cnaes (
    codigo    CHAR(7) PRIMARY KEY,
    descricao TEXT
);

CREATE TABLE IF NOT EXISTS cnpj.rf_municipios (
    codigo    CHAR(7) PRIMARY KEY,
    descricao TEXT
);

CREATE TABLE IF NOT EXISTS cnpj.rf_naturezas (
    codigo    CHAR(4) PRIMARY KEY,
    descricao TEXT
);

CREATE TABLE IF NOT EXISTS cnpj.rf_qualificacoes (
    codigo    CHAR(2) PRIMARY KEY,
    descricao TEXT
);

CREATE TABLE IF NOT EXISTS cnpj.rf_motivos (
    codigo    CHAR(2) PRIMARY KEY,
    descricao TEXT
);

CREATE TABLE IF NOT EXISTS cnpj.rf_paises (
    codigo    CHAR(3) PRIMARY KEY,
    descricao TEXT
);

CREATE TABLE IF NOT EXISTS cnpj.rf_portes (
    codigo    CHAR(2) PRIMARY KEY,
    descricao TEXT
);

-- ── Materialized view for fast querying ─────────────────────

CREATE MATERIALIZED VIEW IF NOT EXISTS cnpj.mv_cnpj_full AS
SELECT
    e.cnpj_basico || est.cnpj_ordem || est.cnpj_dv  AS cnpj_completo,
    e.cnpj_basico,
    est.cnpj_ordem,
    est.cnpj_dv,
    e.razao_social,
    est.nome_fantasia,
    e.natureza_juridica,
    nat.descricao                                    AS natureza_juridica_descricao,
    e.porte,
    prt.descricao                                    AS porte_descricao,
    e.capital_social,
    est.situacao_cadastral,
    mot.descricao                                    AS motivo_situacao_descricao,
    est.data_inicio_atividade,
    est.cnae_fiscal,
    cnae.descricao                                   AS cnae_fiscal_descricao,
    est.cnae_fiscal_secundaria,
    est.tipo_logradouro,
    est.logradouro,
    est.numero,
    est.complemento,
    est.bairro,
    est.cep,
    est.uf,
    est.municipio,
    mun.descricao                                    AS municipio_descricao,
    est.ddd1,
    est.telefone1,
    est.correio_eletronico,
    est.identificador_matriz_filial,
    e.run_key
FROM cnpj.rf_estabelecimentos est
JOIN cnpj.rf_empresas          e    ON e.cnpj_basico      = est.cnpj_basico
LEFT JOIN cnpj.rf_cnaes        cnae ON cnae.codigo         = est.cnae_fiscal
LEFT JOIN cnpj.rf_municipios   mun  ON mun.codigo          = est.municipio
LEFT JOIN cnpj.rf_naturezas    nat  ON nat.codigo          = e.natureza_juridica
LEFT JOIN cnpj.rf_motivos      mot  ON mot.codigo          = est.motivo_situacao_cadastral
LEFT JOIN cnpj.rf_portes       prt  ON prt.codigo          = e.porte
WITH NO DATA;

-- ── Indexes ──────────────────────────────────────────────────

-- Primary lookup
CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_cnpj_completo
    ON cnpj.mv_cnpj_full (cnpj_completo);

CREATE INDEX IF NOT EXISTS idx_mv_cnpj_basico
    ON cnpj.mv_cnpj_full (cnpj_basico);

-- Text search (trigram)
CREATE INDEX IF NOT EXISTS idx_mv_razao_social_trgm
    ON cnpj.mv_cnpj_full USING gin (razao_social gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_mv_nome_fantasia_trgm
    ON cnpj.mv_cnpj_full USING gin (nome_fantasia gin_trgm_ops);

-- Filter indexes
CREATE INDEX IF NOT EXISTS idx_mv_municipio
    ON cnpj.mv_cnpj_full (municipio);

CREATE INDEX IF NOT EXISTS idx_mv_uf
    ON cnpj.mv_cnpj_full (uf);

CREATE INDEX IF NOT EXISTS idx_mv_cnae_fiscal
    ON cnpj.mv_cnpj_full (cnae_fiscal);

CREATE INDEX IF NOT EXISTS idx_mv_situacao_cadastral
    ON cnpj.mv_cnpj_full (situacao_cadastral);

-- Source table indexes
CREATE INDEX IF NOT EXISTS idx_rf_estab_cnpj_basico
    ON cnpj.rf_estabelecimentos (cnpj_basico);

CREATE INDEX IF NOT EXISTS idx_rf_socios_cnpj_basico
    ON cnpj.rf_socios (cnpj_basico);
```

**Step 2: Commit**

```bash
git add db/schema.sql
git commit -m "feat: PostgreSQL schema — tables, materialized view, and indexes"
```

---

## Task 12: Load Step

**Files:**
- Create: `steps/load_step.py`

**Step 1: Create `steps/load_step.py`**

```python
"""
Load step — bulk load transformed CSVs into PostgreSQL using COPY.
Uses psycopg3 (sync) with COPY for maximum throughput.
Truncates + reloads per run_key (idempotent).
"""
from __future__ import annotations

import json
from pathlib import Path

import psycopg
from psycopg import sql

from config import settings
from logger import get_logger
from steps.base import StepResult

log = get_logger("step.load")

TABLE_MAP = {
    "empresas":       "cnpj.rf_empresas",
    "estabelecimentos": "cnpj.rf_estabelecimentos",
    "socios":         "cnpj.rf_socios",
    "cnaes":          "cnpj.rf_cnaes",
    "municipios":     "cnpj.rf_municipios",
    "naturezas":      "cnpj.rf_naturezas",
    "qualificacoes":  "cnpj.rf_qualificacoes",
    "motivos":        "cnpj.rf_motivos",
    "paises":         "cnpj.rf_paises",
    "portes":         "cnpj.rf_portes",
}

# Tables that have run_key column (data tables vs lookup tables)
DATA_TABLES = {"empresas", "estabelecimentos", "socios"}

# Lookup tables are small — DELETE + reload entirely
LOOKUP_TABLES = {"cnaes", "municipios", "naturezas", "qualificacoes", "motivos", "paises", "portes"}


def _copy_csv(conn: psycopg.Connection, table: str, csv_path: Path, run_key: str) -> int:
    """COPY csv_path into table. Returns row count."""
    import csv as csv_mod

    # Read header from CSV
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv_mod.reader(f)
        header = next(reader)

    # Append run_key to data tables
    tbl_short = table.split(".")[-1].replace("rf_", "")
    is_data = tbl_short in DATA_TABLES

    if is_data:
        columns = header + ["run_key"]
    else:
        columns = header

    col_list = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    copy_sql = sql.SQL("COPY {tbl} ({cols}) FROM STDIN WITH (FORMAT csv, HEADER true)").format(
        tbl=sql.SQL(table),
        cols=col_list,
    )

    count = 0
    with conn.cursor() as cur:
        if is_data:
            # Need to inject run_key — use a temp table approach
            # Simpler: write a new CSV with run_key appended
            import io, tempfile, os
            tmp_fd, tmp_name = tempfile.mkstemp(suffix=".csv", dir=csv_path.parent)
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="") as tmp_f:
                    import csv as csv_mod2
                    writer = csv_mod2.writer(tmp_f)
                    writer.writerow(columns)
                    with open(csv_path, "r", encoding="utf-8", newline="") as src:
                        src_reader = csv_mod2.reader(src)
                        next(src_reader)  # skip header
                        for row in src_reader:
                            writer.writerow(row + [run_key])
                            count += 1

                with open(tmp_name, "r", encoding="utf-8") as tmp_f:
                    with cur.copy(copy_sql) as copy:
                        while data := tmp_f.read(65536):
                            copy.write(data)
            finally:
                Path(tmp_name).unlink(missing_ok=True)
        else:
            with open(csv_path, "r", encoding="utf-8") as f:
                with cur.copy(copy_sql) as copy:
                    while data := f.read(65536):
                        copy.write(data)
            # Count from header skip
            count = cur.rowcount if cur.rowcount >= 0 else 0

    return count


def run(job_id: str, run_key: str, checkpoint_dir: Path) -> StepResult:
    transform_artifact = checkpoint_dir / "transform_manifest.json"
    if not transform_artifact.exists():
        return StepResult.failed("transform_manifest.json missing")

    manifest = json.loads(transform_artifact.read_text(encoding="utf-8"))
    out_dir = Path(manifest["out_dir"])

    results = []
    try:
        with psycopg.connect(settings.postgres_url, autocommit=False) as conn:
            for table_entry in manifest["tables"]:
                keyword = table_entry["keyword"]
                tbl_short = keyword.lower()
                pg_table = TABLE_MAP.get(tbl_short)

                if not pg_table:
                    log.warning(f"no table mapping for keyword '{keyword}' — skipping")
                    continue

                csv_path = out_dir / f"{keyword.lower()}.csv"
                if not csv_path.exists():
                    log.warning(f"transformed CSV not found: {csv_path}")
                    continue

                log.info(f"loading {pg_table} from {csv_path.name}...")

                with conn.cursor() as cur:
                    if tbl_short in DATA_TABLES:
                        # Delete existing data for this run_key (idempotent reload)
                        cur.execute(
                            f"DELETE FROM {pg_table} WHERE run_key = %s", (run_key,)
                        )
                    elif tbl_short in LOOKUP_TABLES:
                        cur.execute(
                            f"TRUNCATE {pg_table} RESTART IDENTITY CASCADE"
                        )

                count = _copy_csv(conn, pg_table, csv_path, run_key)
                conn.commit()
                log.info(f"  loaded {count:,} rows into {pg_table}")
                results.append({"table": pg_table, "rows": count})

    except Exception as exc:
        import traceback
        return StepResult.failed(f"load failed: {exc}\n{traceback.format_exc()}")

    artifact = checkpoint_dir / "load_manifest.json"
    tmp = artifact.with_suffix(".tmp")
    tmp.write_text(json.dumps({"run_key": run_key, "tables": results}, indent=2), encoding="utf-8")
    tmp.replace(artifact)

    total = sum(r["rows"] for r in results)
    log.info(f"load complete: {total:,} total rows across {len(results)} tables")
    return StepResult.success(artifact_path=artifact, total_rows=total)
```

**Step 2: Commit**

```bash
git add steps/load_step.py
git commit -m "feat: load step — COPY bulk into Postgres with idempotent reload"
```

---

## Task 13: Index + Cleanup Steps

**Files:**
- Create: `steps/index_step.py`
- Create: `steps/cleanup_step.py`

**Step 1: Create `steps/index_step.py`**

```python
"""
Index step — refresh materialized view and ensure indexes exist.
"""
from __future__ import annotations

import json
from pathlib import Path

import psycopg

from config import settings
from logger import get_logger
from steps.base import StepResult

log = get_logger("step.index")


def run(job_id: str, run_key: str, checkpoint_dir: Path) -> StepResult:
    try:
        with psycopg.connect(settings.postgres_url, autocommit=True) as conn:
            log.info("refreshing materialized view cnpj.mv_cnpj_full...")
            conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY cnpj.mv_cnpj_full")
            log.info("materialized view refreshed")

            # Count rows for validation
            row = conn.execute("SELECT COUNT(*) FROM cnpj.mv_cnpj_full").fetchone()
            count = row[0] if row else 0
            log.info(f"mv_cnpj_full has {count:,} rows")

            if count == 0:
                return StepResult.failed("materialized view is empty after refresh — check load step")

    except Exception as exc:
        import traceback
        return StepResult.failed(f"index step failed: {exc}\n{traceback.format_exc()}")

    artifact = checkpoint_dir / "index_manifest.json"
    tmp = artifact.with_suffix(".tmp")
    tmp.write_text(json.dumps({"run_key": run_key, "mv_rows": count}, indent=2), encoding="utf-8")
    tmp.replace(artifact)

    return StepResult.success(artifact_path=artifact, mv_rows=count)
```

**Step 2: Create `steps/cleanup_step.py`**

```python
"""
Cleanup step — remove temp files, write ultimo_status.json.
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


def run(job_id: str, run_key: str, checkpoint_dir: Path) -> StepResult:
    # Remove raw ZIPs and intermediate CSVs (keep transformed CSVs for audit)
    data_dir = settings.data_dir / run_key
    removed = 0
    for p in data_dir.glob("*.zip"):
        p.unlink(missing_ok=True)
        removed += 1
    for p in (data_dir / "csv").glob("*") if (data_dir / "csv").exists() else []:
        p.unlink(missing_ok=True)
        removed += 1

    log.info(f"removed {removed} temp files")

    # Write ultimo_status.json
    runs = list_runs(limit=5)
    status = {
        "last_run_key": run_key,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": "SUCCESS",
        "recent_runs": [dict(r) for r in runs],
    }
    tmp = settings.status_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(settings.status_file)

    log.info(f"ultimo_status.json updated")
    return StepResult.success(artifact_path=settings.status_file, removed_files=removed)
```

**Step 3: Commit**

```bash
git add steps/index_step.py steps/cleanup_step.py
git commit -m "feat: index step (mv refresh) and cleanup step"
```

---

## Task 14: FastAPI Application

**Files:**
- Create: `api/__init__.py`
- Create: `api/schemas.py`
- Create: `api/main.py`
- Create: `api/routes/__init__.py`
- Create: `api/routes/cnpj.py`
- Create: `api/routes/search.py`
- Create: `api/routes/health.py`
- Create: `api/routes/runs.py`

**Step 1: Create `api/schemas.py`**

```python
from typing import Optional
from pydantic import BaseModel


class CNPJResponse(BaseModel):
    cnpj_completo: str
    cnpj_basico: str
    razao_social: Optional[str]
    nome_fantasia: Optional[str]
    natureza_juridica: Optional[str]
    natureza_juridica_descricao: Optional[str]
    porte: Optional[str]
    porte_descricao: Optional[str]
    capital_social: Optional[str]
    situacao_cadastral: Optional[str]
    motivo_situacao_descricao: Optional[str]
    data_inicio_atividade: Optional[str]
    cnae_fiscal: Optional[str]
    cnae_fiscal_descricao: Optional[str]
    logradouro: Optional[str]
    numero: Optional[str]
    complemento: Optional[str]
    bairro: Optional[str]
    cep: Optional[str]
    uf: Optional[str]
    municipio: Optional[str]
    municipio_descricao: Optional[str]
    telefone1: Optional[str]
    correio_eletronico: Optional[str]
    identificador_matriz_filial: Optional[str]
    run_key: Optional[str]


class HealthResponse(BaseModel):
    status: str
    version: str
    last_success_run_key: Optional[str]
    mv_row_count: Optional[int]


class RunRecord(BaseModel):
    job_id: str
    run_key: str
    status: str
    attempts: int
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    last_error: Optional[str]
```

**Step 2: Create `api/main.py`**

```python
"""FastAPI app with async psycopg3 connection pool."""
from __future__ import annotations

from contextlib import asynccontextmanager

import psycopg_pool
from fastapi import FastAPI

from api.routes import cnpj, health, runs, search
from config import settings

_pool: psycopg_pool.AsyncConnectionPool | None = None


async def get_pool() -> psycopg_pool.AsyncConnectionPool:
    return _pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    _pool = psycopg_pool.AsyncConnectionPool(
        conninfo=settings.postgres_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        open=False,
    )
    await _pool.open()
    app.state.pool = _pool
    yield
    await _pool.close()


app = FastAPI(
    title="ETL CNPJ — API de Consulta",
    description="Consulta de CNPJs com dados da Receita Federal",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(cnpj.router, prefix="/cnpj", tags=["CNPJ"])
app.include_router(search.router, prefix="/search", tags=["Busca"])
app.include_router(health.router, prefix="/health", tags=["Health"])
app.include_router(runs.router, prefix="/runs", tags=["Runs"])
```

**Step 3: Create `api/routes/cnpj.py`**

```python
from fastapi import APIRouter, HTTPException, Request
from api.schemas import CNPJResponse

router = APIRouter()

_SELECT = """
    SELECT * FROM cnpj.mv_cnpj_full
    WHERE cnpj_completo = $1 OR cnpj_basico = $1
    LIMIT 1
"""

@router.get("/{cnpj}", response_model=CNPJResponse)
async def get_cnpj(cnpj: str, request: Request):
    cnpj_clean = "".join(c for c in cnpj if c.isdigit())
    if len(cnpj_clean) not in (8, 14):
        raise HTTPException(400, "CNPJ deve ter 8 (base) ou 14 dígitos")

    async with request.app.state.pool.connection() as conn:
        row = await conn.fetchrow(_SELECT, cnpj_clean)

    if not row:
        raise HTTPException(404, f"CNPJ {cnpj_clean} não encontrado")
    return CNPJResponse(**dict(row))
```

**Step 4: Create `api/routes/search.py`**

```python
from typing import Optional
from fastapi import APIRouter, Request
from api.schemas import CNPJResponse

router = APIRouter()


@router.get("", response_model=list[CNPJResponse])
async def search(
    request: Request,
    razao_social: Optional[str] = None,
    municipio: Optional[str] = None,
    cnae: Optional[str] = None,
    uf: Optional[str] = None,
    situacao: Optional[str] = None,
    limit: int = 20,
):
    limit = min(limit, 100)
    conditions = []
    params = []
    i = 1

    if razao_social:
        conditions.append(f"razao_social ILIKE ${i} OR nome_fantasia ILIKE ${i}")
        params.append(f"%{razao_social}%")
        i += 1
    if municipio:
        conditions.append(f"municipio_descricao ILIKE ${i}")
        params.append(f"%{municipio}%")
        i += 1
    if cnae:
        conditions.append(f"cnae_fiscal = ${i}")
        params.append(cnae)
        i += 1
    if uf:
        conditions.append(f"uf = ${i}")
        params.append(uf.upper())
        i += 1
    if situacao:
        conditions.append(f"situacao_cadastral = ${i}")
        params.append(situacao)
        i += 1

    where = ("WHERE " + " AND ".join(f"({c})" for c in conditions)) if conditions else ""
    query = f"SELECT * FROM cnpj.mv_cnpj_full {where} LIMIT ${i}"
    params.append(limit)

    async with request.app.state.pool.connection() as conn:
        rows = await conn.fetch(query, *params)

    return [CNPJResponse(**dict(r)) for r in rows]
```

**Step 5: Create `api/routes/health.py`**

```python
import json
from fastapi import APIRouter, Request
from api.schemas import HealthResponse

router = APIRouter()


@router.get("", response_model=HealthResponse)
async def health(request: Request):
    from config import settings

    last_run_key = None
    try:
        status = json.loads(settings.status_file.read_text(encoding="utf-8"))
        last_run_key = status.get("last_run_key")
    except Exception:
        pass

    mv_count = None
    try:
        async with request.app.state.pool.connection() as conn:
            row = await conn.fetchrow("SELECT COUNT(*) AS c FROM cnpj.mv_cnpj_full")
            mv_count = row["c"] if row else None
    except Exception:
        pass

    return HealthResponse(
        status="ok",
        version="1.0.0",
        last_success_run_key=last_run_key,
        mv_row_count=mv_count,
    )
```

**Step 6: Create `api/routes/runs.py`**

```python
from fastapi import APIRouter
from api.schemas import RunRecord
from db.control import list_runs

router = APIRouter()


@router.get("", response_model=list[RunRecord])
def get_runs(limit: int = 10):
    limit = min(limit, 50)
    rows = list_runs(limit=limit)
    return [RunRecord(**dict(r)) for r in rows]
```

**Step 7: Create `api/routes/__init__.py`** and `api/__init__.py`** (empty files)**

**Step 8: Commit**

```bash
git add api/
git commit -m "feat: FastAPI app with async pool, CNPJ lookup, search, health, and runs endpoints"
```

---

## Task 15: Task Scheduler Scripts

**Files:**
- Create: `scheduler/install_tasks.ps1`
- Create: `scheduler/README_scheduler.md`
- Create: `start_worker.bat`
- Create: `start_api.bat`

**Step 1: Create `start_worker.bat`**

```bat
@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python worker.py
```

**Step 2: Create `start_api.bat`**

```bat
@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
gunicorn api.main:app -w 8 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000 --timeout 120 --keep-alive 5
```

**Step 3: Create `scheduler/install_tasks.ps1`**

```powershell
# Run as Administrator:
# powershell -ExecutionPolicy Bypass -File scheduler\install_tasks.ps1

param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonExe  = "$ProjectDir\.venv\Scripts\python.exe"
)

$EnqueueScript = "$ProjectDir\enqueue_job.py"
$WorkerBat     = "$ProjectDir\start_worker.bat"
$ApiBat        = "$ProjectDir\start_api.bat"

# Task 1: Check for new month daily at 06:00
$action1  = New-ScheduledTaskAction -Execute $PythonExe -Argument $EnqueueScript -WorkingDirectory $ProjectDir
$trigger1 = New-ScheduledTaskTrigger -Daily -At "06:00"
$settings1 = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName "ETL_CNPJ_Enqueue" `
    -Action $action1 -Trigger $trigger1 -Settings $settings1 `
    -Description "Verifica novo mês da Receita Federal e enfileira job" `
    -RunLevel Highest -Force

Write-Host "OK: ETL_CNPJ_Enqueue criada"

# Task 2: Worker — start at system boot, keep running
$action2  = New-ScheduledTaskAction -Execute $WorkerBat -WorkingDirectory $ProjectDir
$trigger2 = New-ScheduledTaskTrigger -AtStartup
$settings2 = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 7) `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 2) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "ETL_CNPJ_Worker" `
    -Action $action2 -Trigger $trigger2 -Settings $settings2 `
    -Description "Worker ETL CNPJ — roda continuamente" `
    -RunLevel Highest -Force

Write-Host "OK: ETL_CNPJ_Worker criada"

# Task 3: API — start at system boot
$action3  = New-ScheduledTaskAction -Execute $ApiBat -WorkingDirectory $ProjectDir
$trigger3 = New-ScheduledTaskTrigger -AtStartup
$settings3 = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 365) `
    -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "ETL_CNPJ_API" `
    -Action $action3 -Trigger $trigger3 -Settings $settings3 `
    -Description "API FastAPI de consulta CNPJ" `
    -RunLevel Highest -Force

Write-Host "OK: ETL_CNPJ_API criada"
Write-Host ""
Write-Host "Tarefas instaladas. Reinicie o servidor ou inicie manualmente:"
Write-Host "  Start-ScheduledTask -TaskName ETL_CNPJ_Worker"
Write-Host "  Start-ScheduledTask -TaskName ETL_CNPJ_API"
```

**Step 4: Create `scheduler/README_scheduler.md`**

```markdown
# Task Scheduler — Instruções

## Pré-requisitos
- Python 3.11+ instalado
- PowerShell como Administrador
- Projeto configurado com `.env` válido

## Instalar as tarefas

```powershell
# Abra PowerShell como Administrador
powershell -ExecutionPolicy Bypass -File scheduler\install_tasks.ps1
```

## Tarefas criadas

| Tarefa | Gatilho | Descrição |
|---|---|---|
| `ETL_CNPJ_Enqueue` | Diário às 06:00 | Verifica novo mês e enfileira job |
| `ETL_CNPJ_Worker` | Inicialização do servidor | Worker processa jobs da fila |
| `ETL_CNPJ_API` | Inicialização do servidor | API FastAPI na porta 8000 |

## Iniciar manualmente

```powershell
Start-ScheduledTask -TaskName "ETL_CNPJ_Worker"
Start-ScheduledTask -TaskName "ETL_CNPJ_API"
Start-ScheduledTask -TaskName "ETL_CNPJ_Enqueue"
```

## Ver logs

```
logs\etl_YYYY-MM-DD.log
```

## Executar primeiro run manualmente

```bash
# No prompt com .venv ativado
python enqueue_job.py --run-key 2026-02
python worker.py --once
```
```

**Step 5: Commit**

```bash
git add scheduler/ start_worker.bat start_api.bat
git commit -m "feat: Task Scheduler scripts and startup batch files"
```

---

## Task 16: Setup Final e Testes de Fumaça

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_control_db.py`
- Create: `tests/test_config.py`
- Create: `setup.py` (script de setup inicial)

**Step 1: Create `tests/test_control_db.py`**

```python
import pytest
from pathlib import Path
from unittest.mock import patch


def test_create_and_get_job(tmp_path):
    with patch("config.settings.control_db", tmp_path / "test.db"):
        from db.control import init_db, create_job, get_job_by_run_key
        init_db()
        job_id = create_job("2026-01", {"test": True})
        assert job_id is not None
        row = get_job_by_run_key("2026-01")
        assert row["status"] == "PENDING"
        assert row["run_key"] == "2026-01"


def test_acquire_job(tmp_path):
    with patch("config.settings.control_db", tmp_path / "test.db"):
        from db.control import init_db, create_job, acquire_job
        init_db()
        create_job("2026-02")
        row = acquire_job(lease_seconds=60)
        assert row is not None
        assert row["status"] == "RUNNING"  # after acquire
        # Should not acquire again
        row2 = acquire_job()
        assert row2 is None


def test_step_tracking(tmp_path):
    with patch("config.settings.control_db", tmp_path / "test.db"):
        from db.control import init_db, create_job, upsert_step, get_step
        init_db()
        job_id = create_job("2026-03")
        upsert_step(job_id, "download", "SUCCESS", artifact_path="/tmp/art.json")
        step = get_step(job_id, "download")
        assert step["status"] == "SUCCESS"
        assert step["artifact_path"] == "/tmp/art.json"
```

**Step 2: Create `tests/test_config.py`**

```python
def test_settings_load():
    from config import settings
    assert settings.postgres_url is not None
    assert settings.webdav_token != ""
    assert settings.max_job_attempts > 0


def test_ensure_dirs(tmp_path):
    from unittest.mock import patch
    with patch("config.settings.data_dir", tmp_path / "data"), \
         patch("config.settings.log_dir", tmp_path / "logs"), \
         patch("config.settings.checkpoint_dir", tmp_path / "checkpoints"):
        from config import settings
        settings.ensure_dirs()
        assert (tmp_path / "data").exists()
        assert (tmp_path / "logs").exists()
        assert (tmp_path / "checkpoints").exists()
```

**Step 3: Create `setup.py`**

```python
#!/usr/bin/env python
"""
One-time setup: create Postgres schema and initialize SQLite.
Run after configuring .env.

    python setup.py
"""
import subprocess
import sys
from pathlib import Path

from config import settings
from db.control import init_db
from logger import get_logger

log = get_logger("setup")


def create_pg_schema() -> None:
    schema_file = Path(__file__).parent / "db" / "schema.sql"
    log.info(f"applying Postgres schema: {schema_file}")
    result = subprocess.run(
        ["psql", settings.postgres_url, "-f", str(schema_file)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error(f"psql error: {result.stderr}")
        sys.exit(1)
    log.info("Postgres schema applied successfully")


def main() -> None:
    settings.ensure_dirs()
    log.info("initializing SQLite control database...")
    init_db()
    log.info("SQLite OK")

    log.info("creating PostgreSQL schema...")
    create_pg_schema()
    log.info("Setup complete!")
    print("\n✅ Setup completo! Próximos passos:")
    print("   python enqueue_job.py --run-key 2026-02")
    print("   python worker.py --once")
    print("   python start_api.bat")


if __name__ == "__main__":
    main()
```

**Step 4: Run tests**

```bash
pip install pytest
pytest tests/ -v
```

Expected: all tests pass.

**Step 5: Commit**

```bash
git add tests/ setup.py steps/__init__.py api/__init__.py api/routes/__init__.py db/__init__.py
git commit -m "feat: smoke tests and setup script — project complete"
```

---

## Quick Start (após clonar/criar o projeto)

```bash
# 1. Ambiente virtual
python -m venv .venv
.venv\Scripts\activate

# 2. Dependências
pip install -r requirements.txt

# 3. Configurar
copy .env.example .env
# editar .env com POSTGRES_URL e demais variáveis

# 4. Setup (cria schema Postgres + SQLite)
python setup.py

# 5. Primeiro run
python enqueue_job.py --run-key 2026-02
python worker.py --once

# 6. API
start_api.bat
# Acesse: http://localhost:8000/docs

# 7. Task Scheduler (PowerShell Admin)
powershell -ExecutionPolicy Bypass -File scheduler\install_tasks.ps1
```
