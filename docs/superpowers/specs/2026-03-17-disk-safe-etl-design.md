# Design: ETL Disk-Safe Update Cycle

**Date:** 2026-03-17
**Status:** Approved
**Context:** On 2026-03-16, the `index_step` failed with `No space left on device` during `REFRESH MATERIALIZED VIEW CONCURRENTLY`. Root cause: ~40 GB of `_old` tables + ~20 GB of ETL files (ZIPs/CSVs) occupied the disk simultaneously, leaving insufficient temp space for PostgreSQL's CONCURRENTLY sort.

---

## Problem

During the monthly ETL cycle, disk occupancy peaks at:

| What | Size |
|------|------|
| New tables (load_step) | ~60 GB |
| `_old` tables (kept until after REFRESH) | ~40 GB |
| Raw ZIPs + intermediate CSVs | ~20 GB |
| PostgreSQL temp for REFRESH CONCURRENTLY | ~40 GB needed |

Total needed: ~160 GB. With 193 GB disk, only ~22 GB was free at the time of failure — not enough.

---

## Solution

Two targeted changes. No changes to `orchestrator.py` or `cleanup_step.py`.

### Change 1 — load_step: delete ETL files after swap

At the end of `load_step.run()`, after all table swaps succeed and **after the `psycopg.connect` block exits**, delete:
- Raw ZIPs (`*.zip`, `*.part`) from `/data/{run_key}/`
- Intermediate CSVs from `/data/{run_key}/csv/`
- Keep `/data/{run_key}/transformed/` (audit trail)

This frees ~20 GB **before** `index_step` runs, without changing step ordering or affecting `ultimo_status.json` (still written by `cleanup_step` at the end).

**Sequencing:** The deletion must happen after the `with psycopg.connect(...)` block closes, between the database block and the `write_artifact` call. This prevents any `OSError` during file deletion from being caught by the DB exception handler and marking the step as FAILED with tables already swapped.

**Error handling:** File deletion errors must be non-fatal — caught and logged as `WARNING`, never raised. Mirror the exact pattern of `cleanup_step._remove_glob()` (line 27). This keeps `load_step` idempotent: if the pipeline is retried after a partial deletion, `cleanup_step` will clean up any remaining files at the end.

**Implementation:** Inline glob-delete in `load_step` (do not couple to `cleanup_step`). Pattern to follow: iterate `directory.glob(pattern)`, call `p.unlink()`, catch `Exception` per file.

### Change 2 — index_step: disk check + fallback before REFRESH CONCURRENTLY

Before attempting `REFRESH MATERIALIZED VIEW CONCURRENTLY`, check available disk space via `shutil.disk_usage()`.

**Threshold:** `settings.index_min_free_gb` (default: `50`, type: `int`). Add to `config.py` / `Settings`. The value 50 GB is conservative: REFRESH needs ~40 GB temp; 10 GB is margin. With the planned VPS upgrade to 300 GB, this can be revised via env var without touching code.

**Filesystem path:** Check `shutil.disk_usage(settings.data_dir)` — the same volume where PostgreSQL data lives in this Docker setup (both on `/dev/sda1`). Do NOT hardcode `"/"` as that assumption may break if volumes are separated in the future. `settings.data_dir` is already a configured path and maps to the correct disk.

**Decision logic:**

```
free_gb = shutil.disk_usage(settings.data_dir).free / 1024**3

if status == "populated":
    if free_gb >= settings.index_min_free_gb:
        REFRESH MATERIALIZED VIEW CONCURRENTLY   # non-blocking, normal path
    else:
        log WARNING: "disco livre={free_gb:.0f}GB < {settings.index_min_free_gb}GB — fallback: DROP CASCADE + full rebuild"
        DROP MATERIALIZED VIEW cnpj_serving.mv_cnpj_full CASCADE
        _recreate_mv(conn)          # creates MV WITH NO DATA + all indexes (incl. unique idx for future CONCURRENTLY)
        REFRESH MATERIALIZED VIEW   # blocking, ~45-90 min, stale cache covers readers
```

**Important:** The fallback must call `_recreate_mv()` (not inline DDL) to ensure all indexes — including `idx_mv_cnpj_completo` (UNIQUE) — are created before the blocking REFRESH. The UNIQUE index is required for `REFRESH CONCURRENTLY` on the next monthly cycle.

For `status == "missing"` or `"empty"`, existing non-concurrent path is unchanged.

**During fallback:** API serves stale cache (90-day TTL, `cache_stale_ttl: 7776000` in `config.py`). No intervention needed.

---

## What Does NOT Change

- `orchestrator.py` — step order unchanged: `load → index → cleanup`
- `cleanup_step.py` — still runs last, writes `ultimo_status.json`, cleans any leftover files
- `db/control.py`, `worker.py`, API routes — untouched
- MV DDL, indexes, Redis flush — untouched

---

## Disk Budget After Changes

### Normal path (REFRESH CONCURRENTLY)

| What | Size |
|------|------|
| New tables (live) | ~60 GB |
| `_old` tables | ~40 GB |
| ETL files | **0 GB** (deleted by load_step) |
| PostgreSQL temp for REFRESH CONCURRENTLY | ~40 GB |
| **Free at REFRESH time (193 GB disk)** | **~53 GB** ✓ |

### Fallback path (DROP CASCADE + full rebuild)

Even the non-concurrent `REFRESH MATERIALIZED VIEW` must write the full MV heap before swapping (~35 GB for 70M rows with the current DDL columns).

| What | Size |
|------|------|
| New tables (live) | ~60 GB |
| `_old` tables | ~40 GB |
| ETL files | **0 GB** (deleted) |
| MV rebuild heap | ~35 GB |
| **Free during fallback rebuild (193 GB disk)** | **~58 GB** ✓ |

Both paths fit comfortably on the current 193 GB disk. With the planned upgrade to 300 GB, free space grows to ~160 GB (normal) / ~165 GB (fallback) — disk becomes a non-issue.

---

## Files Changed

| File | Change |
|------|--------|
| `steps/load_step.py` | Add file cleanup after DB block exits (post-swap) |
| `steps/index_step.py` | Add disk check + fallback before REFRESH CONCURRENTLY |
| `config.py` | Add `index_min_free_gb: int = 50` setting |

---

## Success Criteria

1. After `load_step` SUCCESS, ZIPs and intermediate CSVs are deleted from `/data/{run_key}/`
2. `index_step` logs free disk space before deciding REFRESH strategy
3. If free < 50 GB: fallback executes without manual intervention, API continues serving stale cache
4. If free ≥ 50 GB: REFRESH CONCURRENTLY runs as before (non-blocking)
5. `ultimo_status.json` is still written by `cleanup_step` after index SUCCESS
