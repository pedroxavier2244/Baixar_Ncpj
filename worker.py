"""
Worker — polls job queue, acquires lease, runs pipeline with heartbeat.

Usage:
    python worker.py            # loop forever
    python worker.py --once     # run one job and exit
    python worker.py --poll-interval 120
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import traceback

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
            log.debug(f"heartbeat renewed for job {job_id}")
        except Exception as exc:
            log.warning(f"heartbeat renew failed for {job_id}: {exc}")


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
        error = traceback.format_exc()
        log.error(f"pipeline crashed for job {job_id}: {exc}")
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
            log.error(f"unexpected worker error: {exc}\n{traceback.format_exc()}")
            time.sleep(30)


if __name__ == "__main__":
    main()
