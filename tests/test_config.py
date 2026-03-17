def test_settings_load():
    from config import settings
    assert settings.postgres_url is not None
    assert settings.webdav_share_url.startswith("https://")
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


def test_index_defaults():
    from config import Settings
    s = Settings()
    assert s.index_min_free_gb == 50
    assert s.index_work_mem == "2GB"
    assert s.index_parallel_workers == 4
