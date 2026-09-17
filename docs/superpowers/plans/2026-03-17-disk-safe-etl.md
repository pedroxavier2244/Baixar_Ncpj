# Disk-Safe ETL Update Cycle — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent disk-full failures during the monthly ETL by deleting temporary files immediately after load and adding a disk-space fallback in the index step.

**Architecture:** Two targeted changes to existing files. `load_step` deletes ZIPs/CSVs after the database swap completes. `index_step` checks free disk space before `REFRESH CONCURRENTLY` and falls back to DROP CASCADE + full rebuild if space is insufficient. A new `index_work_mem` session setting is applied before every REFRESH to reduce temp file spill.

**Tech Stack:** Python 3.12, psycopg 3, PostgreSQL 16, pydantic-settings, pytest

**Spec:** `docs/superpowers/specs/2026-03-17-disk-safe-etl-design.md`

---

## Chunk 1: config.py — new settings

### Task 1: Add index settings to config

**Files:**
- Modify: `config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_config.py`:

```python
def test_index_defaults():
    from config import Settings
    s = Settings()
    assert s.index_min_free_gb == 50
    assert s.index_work_mem == "2GB"
    assert s.index_parallel_workers == 4
```

- [ ] **Step 2: Run test — confirm FAIL**

```bash
cd "C:\Users\MB NEGOCIOS\Desktop\BANCO CNPJ\Baixar_Ncpj"
python -m pytest tests/test_config.py::test_index_defaults -v
```

Expected: `AttributeError: 'Settings' object has no attribute 'index_min_free_gb'`

- [ ] **Step 3: Add settings to config.py**

In `config.py`, after the `db_query_timeout_ms` line (line 55), add:

```python
    # Index step — disk safety and performance
    index_min_free_gb: int = 50       # mínimo de espaço livre antes do REFRESH CONCURRENTLY
    index_work_mem: str = "2GB"       # work_mem da sessão durante o REFRESH (reduz spill para disco)
    index_parallel_workers: int = 4   # max_parallel_workers_per_gather durante o REFRESH
```

- [ ] **Step 4: Run test — confirm PASS**

```bash
python -m pytest tests/test_config.py::test_index_defaults -v
```

Expected: `PASSED`

- [ ] **Step 5: Run full test suite — confirm no regressions**

```bash
python -m pytest tests/test_config.py -v
```

- [ ] **Step 6: Commit**

```bash
git add config.py tests/test_config.py
git commit -m "feat(config): add index_min_free_gb, index_work_mem, index_parallel_workers"
```

---

## Chunk 2: load_step.py — delete ETL files after swap

### Task 2: Add post-swap file cleanup

**Files:**
- Modify: `steps/load_step.py` (after line 419, outside the `with psycopg.connect` block)
- Test: `tests/test_load_swap_unit.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_load_swap_unit.py`:

```python
def test_run_cleans_etl_files_after_swap(tmp_path):
    """After a successful load, ZIPs and intermediate CSVs must be deleted."""
    import json
    from unittest.mock import patch, MagicMock
    from steps.load_step import run

    # Setup fake run directory with files to clean
    run_key = "2026-03"
    data_dir = tmp_path / run_key
    csv_subdir = data_dir / "csv"
    transformed_dir = data_dir / "transformed"
    csv_subdir.mkdir(parents=True)
    transformed_dir.mkdir(parents=True)

    zip_file = data_dir / "Empresas0.zip"
    part_file = data_dir / "Empresas0.part"
    csv_file = csv_subdir / "empresas.csv"
    transformed_file = transformed_dir / "empresas.csv"

    for f in [zip_file, part_file, csv_file, transformed_file]:
        f.write_text("data")

    # Setup checkpoint with fake manifest
    checkpoint_dir = tmp_path / "checkpoint"
    checkpoint_dir.mkdir()
    manifest = {"out_dir": str(data_dir / "transformed"), "tables": []}
    (checkpoint_dir / "transform_manifest.json").write_text(json.dumps(manifest))

    with patch("steps.load_step.settings") as mock_settings, \
         patch("psycopg.connect") as mock_connect:
        mock_settings.postgres_url = "postgresql://fake"
        mock_settings.data_dir = tmp_path
        mock_settings.pg_schema = "cnpj"
        mock_settings.pg_staging_schema = "cnpj_staging"
        mock_connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        result = run("job1", run_key, checkpoint_dir)

    # ZIPs and intermediate CSVs must be gone
    assert not zip_file.exists(), "ZIP should be deleted after load"
    assert not part_file.exists(), ".part file should be deleted after load"
    assert not csv_file.exists(), "intermediate CSV should be deleted after load"
    # transformed/ must be preserved
    assert transformed_file.exists(), "transformed CSV must NOT be deleted"
```

- [ ] **Step 2: Run test — confirm FAIL**

```bash
python -m pytest tests/test_load_swap_unit.py::test_run_cleans_etl_files_after_swap -v
```

Expected: `FAILED` — files still exist after run.

- [ ] **Step 3: Add `_cleanup_etl_files` helper and call it in `run()`**

In `steps/load_step.py`, add this function before `run()` (around line 314):

```python
def _cleanup_etl_files(run_key: str) -> None:
    """
    Remove ZIPs e CSVs intermediários do run atual após o swap bem-sucedido.
    Mantém o subdiretório transformed/ (auditoria).
    Erros são não-fatais — logados como WARNING.
    """
    data_dir = settings.data_dir / run_key
    removed = 0
    for pattern in ("*.zip", "*.part"):
        for p in data_dir.glob(pattern):
            try:
                p.unlink()
                removed += 1
            except Exception as exc:
                log.warning(f"could not remove {p}: {exc}")
    csv_dir = data_dir / "csv"
    if csv_dir.exists():
        for p in csv_dir.glob("*"):
            try:
                p.unlink()
                removed += 1
            except Exception as exc:
                log.warning(f"could not remove {p}: {exc}")
        try:
            csv_dir.rmdir()
        except OSError:
            pass
    if removed:
        log.info(f"[post-swap cleanup] removed {removed} temp files from {data_dir}")
```

Then in `run()`, after the `except Exception` block (after line 419) and before `artifact = checkpoint_dir / "load_manifest.json"`, add:

```python
    # Libera espaço em disco antes do index_step
    _cleanup_etl_files(run_key)
```

- [ ] **Step 4: Run test — confirm PASS**

```bash
python -m pytest tests/test_load_swap_unit.py::test_run_cleans_etl_files_after_swap -v
```

Expected: `PASSED`

- [ ] **Step 5: Run full load test suite — confirm no regressions**

```bash
python -m pytest tests/test_load_swap_unit.py tests/test_load_helpers.py -v
```

- [ ] **Step 6: Commit**

```bash
git add steps/load_step.py tests/test_load_swap_unit.py
git commit -m "feat(load): delete ZIPs and intermediate CSVs after successful swap"
```

---

## Chunk 3: index_step.py — disk check + work_mem + fallback

### Task 3: Add disk check and session work_mem before REFRESH

**Files:**
- Modify: `steps/index_step.py`
- Test: `tests/test_index_disk_safety.py` (new file)

- [ ] **Step 1: Write failing tests**

Create `tests/test_index_disk_safety.py`:

```python
"""
Unit tests for disk-safety and work_mem logic in index_step.
Does NOT require a PostgreSQL connection — all DB calls are mocked.
"""
from __future__ import annotations
from unittest.mock import MagicMock, patch, call
import pytest


def _make_conn(free_gb: float):
    """Return a mock psycopg connection and mock disk_usage for given free GB."""
    conn = MagicMock()
    conn.execute = MagicMock()
    return conn


def test_concurrent_refresh_when_disk_sufficient():
    """When free disk >= index_min_free_gb, uses REFRESH CONCURRENTLY."""
    from steps.index_step import _choose_refresh_strategy

    conn = MagicMock()
    with patch("steps.index_step.shutil.disk_usage") as mock_du, \
         patch("steps.index_step.settings") as mock_settings:
        mock_du.return_value.free = 60 * 1024 ** 3  # 60 GB
        mock_settings.index_min_free_gb = 50
        mock_settings.data_dir = "/data"

        strategy = _choose_refresh_strategy()

    assert strategy == "concurrent"


def test_fallback_when_disk_insufficient():
    """When free disk < index_min_free_gb, uses fallback (DROP + rebuild)."""
    from steps.index_step import _choose_refresh_strategy

    with patch("steps.index_step.shutil.disk_usage") as mock_du, \
         patch("steps.index_step.settings") as mock_settings:
        mock_du.return_value.free = 30 * 1024 ** 3  # 30 GB
        mock_settings.index_min_free_gb = 50
        mock_settings.data_dir = "/data"

        strategy = _choose_refresh_strategy()

    assert strategy == "fallback"


def test_session_work_mem_set_before_refresh():
    """work_mem and parallel_workers are SET before any REFRESH."""
    from steps.index_step import _apply_session_settings

    conn = MagicMock()
    with patch("steps.index_step.settings") as mock_settings:
        mock_settings.index_work_mem = "2GB"
        mock_settings.index_parallel_workers = 4
        _apply_session_settings(conn)

    calls = [str(c) for c in conn.execute.call_args_list]
    assert any("work_mem" in c and "2GB" in c for c in calls)
    assert any("max_parallel_workers_per_gather" in c and "4" in c for c in calls)
```

- [ ] **Step 2: Run tests — confirm FAIL**

```bash
python -m pytest tests/test_index_disk_safety.py -v
```

Expected: `ImportError` — `_choose_refresh_strategy` and `_apply_session_settings` don't exist yet.

- [ ] **Step 3: Add helpers to index_step.py**

In `steps/index_step.py`, add `import shutil` to the imports section. Then add these two helpers after `_flush_redis_cache()` (around line 198):

```python
def _apply_session_settings(conn: psycopg.Connection) -> None:
    """Aplica work_mem e parallel workers na sessão para reduzir spill para disco."""
    conn.execute(f"SET work_mem = '{settings.index_work_mem}'")
    conn.execute(f"SET max_parallel_workers_per_gather = {settings.index_parallel_workers}")
    log.info(
        f"session settings: work_mem={settings.index_work_mem}, "
        f"max_parallel_workers_per_gather={settings.index_parallel_workers}"
    )


def _choose_refresh_strategy() -> str:
    """
    Retorna 'concurrent' se houver espaço livre suficiente, 'fallback' caso contrário.
    Checa o volume de settings.data_dir (mesmo disco que o PostgreSQL neste setup).
    """
    free_bytes = shutil.disk_usage(settings.data_dir).free
    free_gb = free_bytes / 1024 ** 3
    if free_gb >= settings.index_min_free_gb:
        log.info(f"disco livre: {free_gb:.1f} GB >= {settings.index_min_free_gb} GB — REFRESH CONCURRENTLY")
        return "concurrent"
    log.warning(
        f"disco livre: {free_gb:.1f} GB < {settings.index_min_free_gb} GB — "
        f"fallback: DROP CASCADE + full rebuild"
    )
    return "fallback"
```

- [ ] **Step 4: Run tests — confirm PASS**

```bash
python -m pytest tests/test_index_disk_safety.py -v
```

Expected: `3 passed`

- [ ] **Step 5: Wire helpers into run()**

In `steps/index_step.py`, modify the `run()` function's `else` branch (the `status == "populated"` case, around line 222):

Replace:
```python
            else:
                # Atualização normal — REFRESH CONCURRENTLY não bloqueia leitores
                log.info("refreshing mv_cnpj_full CONCURRENTLY (non-blocking)...")
                conn.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {_V}.mv_cnpj_full")
```

With:
```python
            else:
                # Atualização normal — aplica settings de sessão antes do REFRESH
                _apply_session_settings(conn)
                strategy = _choose_refresh_strategy()

                if strategy == "concurrent":
                    log.info("refreshing mv_cnpj_full CONCURRENTLY (non-blocking)...")
                    conn.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {_V}.mv_cnpj_full")
                else:
                    # Fallback: DROP CASCADE + full rebuild (bloqueante, ~45 min)
                    # stale cache da API cobre leitores durante o rebuild
                    log.warning("executando fallback: DROP CASCADE + full rebuild...")
                    conn.execute(f"DROP MATERIALIZED VIEW IF EXISTS {_V}.mv_cnpj_full CASCADE")
                    _recreate_mv(conn)
                    log.info("refreshing mv_cnpj_full (full rebuild — fallback)...")
                    conn.execute(f"REFRESH MATERIALIZED VIEW {_V}.mv_cnpj_full")
```

Also add `_apply_session_settings(conn)` before the non-concurrent REFRESHes in the `missing` and `empty` branches:

In the `missing` branch (around line 214), before `conn.execute(f"REFRESH MATERIALIZED VIEW {_V}.mv_cnpj_full")`:
```python
                _apply_session_settings(conn)
```

In the `empty` branch (around line 219), before `conn.execute(f"REFRESH MATERIALIZED VIEW {_V}.mv_cnpj_full")`:
```python
                _apply_session_settings(conn)
```

- [ ] **Step 6: Run all index tests**

```bash
python -m pytest tests/test_index_disk_safety.py -v
```

Expected: all pass.

- [ ] **Step 7: Run full test suite**

```bash
python -m pytest tests/ -v --ignore=tests/test_e2e_pipeline.py --ignore=tests/test_e2e_schema_swap.py
```

Expected: all pass (e2e tests skipped — require live DB).

- [ ] **Step 8: Commit**

```bash
git add steps/index_step.py tests/test_index_disk_safety.py
git commit -m "feat(index): disk check + work_mem session settings + fallback para low-disk"
```

---

## Chunk 4: VPS PostgreSQL global settings

### Task 4: Apply persistent PostgreSQL configuration on the VPS

These commands are run **once on the VPS** via `docker exec`. They persist via `postgresql.auto.conf` and take effect after a config reload (no full restart needed for most settings; `shared_buffers` requires restart).

- [ ] **Step 1: Apply settings**

```bash
docker exec implementation-postgres-1 psql -U etl_user -d cnpj_db -c "
ALTER SYSTEM SET shared_buffers = '2GB';
ALTER SYSTEM SET effective_cache_size = '8GB';
ALTER SYSTEM SET checkpoint_completion_target = '0.9';
ALTER SYSTEM SET wal_buffers = '64MB';
ALTER SYSTEM SET maintenance_work_mem = '1GB';
SELECT pg_reload_conf();
"
```

- [ ] **Step 2: Restart PostgreSQL container to apply shared_buffers**

```bash
docker restart implementation-postgres-1
```

Wait ~30s for the container to be healthy:

```bash
docker ps --filter name=implementation-postgres-1 --format "{{.Status}}"
```

Expected: `Up X seconds (healthy)`

- [ ] **Step 3: Verify settings applied**

```bash
docker exec implementation-postgres-1 psql -U etl_user -d cnpj_db -c "
SELECT name, setting, unit FROM pg_settings
WHERE name IN ('shared_buffers','effective_cache_size','work_mem','wal_buffers','maintenance_work_mem')
ORDER BY name;
"
```

- [ ] **Step 4: Confirm API and worker still healthy**

```bash
curl -s http://localhost/health | python3 -m json.tool
docker logs cnpj_worker --tail=5
```

---

## Chunk 5: Deploy and verify

### Task 5: Build and deploy updated containers

- [ ] **Step 1: Rebuild cnpj_worker and cnpj_api images**

```bash
cd /path/to/Baixar_Ncpj   # on the VPS
docker compose build worker api
```

- [ ] **Step 2: Restart with new images**

```bash
docker compose up -d worker api
```

- [ ] **Step 3: Verify load_step cleanup in next run**

After the next monthly ETL run, confirm ZIPs are deleted:

```bash
docker exec cnpj_worker find /data -name "*.zip" | head -5
```

Expected: no output (all ZIPs deleted after load).

- [ ] **Step 4: Verify disk log in index_step**

```bash
docker logs cnpj_worker | grep "disco livre"
```

Expected:
```
[INFO] step.index: disco livre: XX.X GB >= 50 GB — REFRESH CONCURRENTLY
[INFO] step.index: session settings: work_mem=2GB, max_parallel_workers_per_gather=4
```

- [ ] **Step 5: Final commit — update spec status**

```bash
git add docs/superpowers/specs/2026-03-17-disk-safe-etl-design.md
git commit -m "docs: mark disk-safe ETL spec as implemented"
```
