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
    print("\nSetup completo! Proximos passos:")
    print("   python enqueue_job.py --run-key 2026-02")
    print("   python worker.py --once")
    print("   start_api.bat")


if __name__ == "__main__":
    main()
