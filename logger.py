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
            "ts": datetime.utcfromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
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
    logger.propagate = False

    # File handler — JSON, daily rotation, keep 30 days
    fh = TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=30, encoding="utf-8"
    )
    fh.setFormatter(JsonFormatter())
    fh.setLevel(logging.DEBUG)
    logger.addHandler(fh)

    # Stdout handler — human readable
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    sh.setLevel(logging.INFO)
    logger.addHandler(sh)

    return logger
