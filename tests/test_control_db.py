import pytest
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
        from db.control import init_db, create_job, acquire_job, get_job_by_run_key
        init_db()
        create_job("2026-02")
        row = acquire_job(lease_seconds=60)
        assert row is not None
        # acquire_job returns pre-update snapshot; verify DB reflects RUNNING
        updated = get_job_by_run_key("2026-02")
        assert updated["status"] == "RUNNING"
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
