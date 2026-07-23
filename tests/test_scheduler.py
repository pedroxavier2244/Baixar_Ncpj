from unittest.mock import patch


def _fake_items(etag="aaa"):
    """Uma listagem WebDAV mínima com um ZIP cujo nome contém o mês."""
    return [{
        "name": "Empresas_2026-07.zip",
        "path": "Empresas_2026-07.zip",
        "is_dir": False,
        "size": 10,
        "modified": "",
        "etag": etag,
    }]


def _settings_patch(tmp_path):
    """Redireciona control_db, dirs e token para um ambiente isolado."""
    return patch.multiple(
        "config.settings",
        control_db=tmp_path / "control.db",
        checkpoint_dir=tmp_path / "checkpoints",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        webdav_token="fake-token",
    )


def test_check_and_enqueue_creates_job(tmp_path):
    with _settings_patch(tmp_path), \
         patch("enqueue_job.propfind_listing", return_value=_fake_items("aaa")):
        from enqueue_job import check_and_enqueue
        result = check_and_enqueue()
        assert result["status"] == "enqueued"
        assert result["run_key"] == "2026-07"
        assert result["files"] == 1
        assert result["job_id"]


def test_check_and_enqueue_no_change_second_call(tmp_path):
    with _settings_patch(tmp_path), \
         patch("enqueue_job.propfind_listing", return_value=_fake_items("aaa")):
        from enqueue_job import check_and_enqueue
        first = check_and_enqueue()
        assert first["status"] == "enqueued"
        second = check_and_enqueue()
        assert second["status"] == "no_change"
        assert second["run_key"] == "2026-07"


def test_check_and_enqueue_already_success(tmp_path):
    with _settings_patch(tmp_path), \
         patch("enqueue_job.propfind_listing", return_value=_fake_items("aaa")):
        from enqueue_job import check_and_enqueue
        from db.control import finish_job
        first = check_and_enqueue()
        finish_job(first["job_id"], success=True)
        again = check_and_enqueue()
        assert again["status"] == "already_success"
        assert again["run_key"] == "2026-07"


def test_check_and_enqueue_webdav_error(tmp_path):
    def boom(*args, **kwargs):
        raise RuntimeError("connection refused")
    with _settings_patch(tmp_path), \
         patch("enqueue_job.propfind_listing", side_effect=boom):
        from enqueue_job import check_and_enqueue
        result = check_and_enqueue()
        assert result["status"] == "error"
        assert "connection refused" in result["reason"]
