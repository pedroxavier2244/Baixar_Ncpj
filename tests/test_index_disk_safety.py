"""
Unit tests for disk-safety and work_mem logic in index_step.
Does NOT require a PostgreSQL connection — all DB calls are mocked.
"""
from __future__ import annotations
from unittest.mock import MagicMock, patch
import pytest


def test_concurrent_refresh_when_disk_sufficient():
    """When free disk >= index_min_free_gb, returns 'concurrent'."""
    from steps.index_step import _choose_refresh_strategy

    with patch("steps.index_step.shutil") as mock_shutil, \
         patch("steps.index_step.settings") as mock_settings:
        mock_shutil.disk_usage.return_value.free = 60 * 1024 ** 3  # 60 GB
        mock_settings.index_min_free_gb = 50
        mock_settings.data_dir = "/data"

        strategy = _choose_refresh_strategy()

    assert strategy == "concurrent"


def test_fallback_when_disk_insufficient():
    """When free disk < index_min_free_gb, returns 'fallback'."""
    from steps.index_step import _choose_refresh_strategy

    with patch("steps.index_step.shutil") as mock_shutil, \
         patch("steps.index_step.settings") as mock_settings:
        mock_shutil.disk_usage.return_value.free = 30 * 1024 ** 3  # 30 GB
        mock_settings.index_min_free_gb = 50
        mock_settings.data_dir = "/data"

        strategy = _choose_refresh_strategy()

    assert strategy == "fallback"


def test_session_work_mem_set_before_refresh():
    """work_mem and parallel_workers are SET on the connection."""
    from steps.index_step import _apply_session_settings

    conn = MagicMock()
    with patch("steps.index_step.settings") as mock_settings:
        mock_settings.index_work_mem = "2GB"
        mock_settings.index_parallel_workers = 4
        _apply_session_settings(conn)

    calls = [str(c) for c in conn.execute.call_args_list]
    assert any("work_mem" in c and "2GB" in c for c in calls)
    assert any("max_parallel_workers_per_gather" in c and "4" in c for c in calls)
