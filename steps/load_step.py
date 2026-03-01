"""
Load step — bulk load transformed CSVs into PostgreSQL using COPY.
Idempotent: deletes existing run_key data before reloading.
Uses psycopg3 COPY for maximum throughput (no row-by-row inserts).
"""
from __future__ import annotations

import csv
import json
import os
import tempfile
from pathlib import Path

import psycopg

from config import settings
from logger import get_logger
from steps.base import StepResult

log = get_logger("step.load")

# Maps transformed CSV keyword to PostgreSQL table
TABLE_MAP: dict[str, str] = {
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

# Tables that carry run_key and should be deleted by run_key (not truncated)
DATA_TABLES: frozenset[str] = frozenset({"empresas", "estabelecimentos", "socios"})

# Lookup tables are small — safe to TRUNCATE and reload entirely
LOOKUP_TABLES: frozenset[str] = frozenset({
    "cnaes", "municipios", "naturezas", "qualificacoes", "motivos", "paises", "portes"
})


def _read_header(csv_path: Path) -> list[str]:
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        return next(reader)


def _build_copy_with_run_key(csv_path: Path, run_key: str) -> Path:
    """
    Write a temporary CSV that appends run_key as the last column on every row.
    Returns path to the temp file.
    """
    fd, tmp_name = tempfile.mkstemp(suffix=".csv", dir=csv_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as out_f:
            writer = csv.writer(out_f)
            with open(csv_path, "r", encoding="utf-8", newline="") as in_f:
                reader = csv.reader(in_f)
                header = next(reader)
                writer.writerow(header + ["run_key"])
                for row in reader:
                    writer.writerow(row + [run_key])
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return Path(tmp_name)


def _copy_table(conn: psycopg.Connection, pg_table: str, csv_path: Path,
                columns: list[str]) -> int:
    """Stream csv_path into pg_table via COPY. Returns approximate row count."""
    col_sql = ", ".join(f'"{c}"' for c in columns)
    copy_sql = f'COPY {pg_table} ({col_sql}) FROM STDIN WITH (FORMAT csv, HEADER true)'

    rows = 0
    with conn.cursor() as cur:
        with cur.copy(copy_sql) as copy:
            with open(csv_path, "r", encoding="utf-8") as f:
                while True:
                    chunk = f.read(65536)  # 64 KB per send
                    if not chunk:
                        break
                    copy.write(chunk)
                    rows += chunk.count("\n")
    # subtract 1 for header line
    return max(0, rows - 1)


def _load_table(conn: psycopg.Connection, tbl_key: str, pg_table: str,
                csv_path: Path, run_key: str) -> int:
    header = _read_header(csv_path)

    if tbl_key in DATA_TABLES:
        # Delete existing rows for this run_key before reload (idempotent)
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {pg_table} WHERE run_key = %s", (run_key,))
        # Append run_key column and COPY
        tmp_path = _build_copy_with_run_key(csv_path, run_key)
        try:
            count = _copy_table(conn, pg_table, tmp_path, header + ["run_key"])
        finally:
            tmp_path.unlink(missing_ok=True)
    else:
        # Lookup tables: truncate + reload (small, fast, idempotent)
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE {pg_table} RESTART IDENTITY CASCADE")
        count = _copy_table(conn, pg_table, csv_path, header)

    return count


def run(job_id: str, run_key: str, checkpoint_dir: Path) -> StepResult:
    transform_artifact = checkpoint_dir / "transform_manifest.json"
    if not transform_artifact.exists():
        return StepResult.failed("transform_manifest.json missing — run transform step first")

    manifest = json.loads(transform_artifact.read_text(encoding="utf-8"))
    out_dir = Path(manifest["out_dir"])

    results: list[dict] = []

    try:
        with psycopg.connect(settings.postgres_url, autocommit=False) as conn:
            for table_entry in manifest.get("tables", []):
                keyword = table_entry["keyword"]
                tbl_key = keyword.lower()
                pg_table = TABLE_MAP.get(tbl_key)

                if not pg_table:
                    log.warning(f"no TABLE_MAP entry for keyword '{keyword}' — skipping")
                    continue

                csv_path = out_dir / f"{keyword.lower()}.csv"
                if not csv_path.exists():
                    log.warning(f"transformed CSV not found: {csv_path} — skipping")
                    continue

                log.info(f"loading {pg_table} from {csv_path.name}...")
                count = _load_table(conn, tbl_key, pg_table, csv_path, run_key)
                conn.commit()
                log.info(f"  loaded ~{count:,} rows into {pg_table}")
                results.append({"table": pg_table, "approx_rows": count})

    except Exception as exc:
        import traceback
        return StepResult.failed(f"load failed: {exc}\n{traceback.format_exc()}")

    artifact = checkpoint_dir / "load_manifest.json"
    tmp = artifact.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"run_key": run_key, "tables": results}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(artifact)

    total = sum(r["approx_rows"] for r in results)
    log.info(f"load complete: ~{total:,} total rows across {len(results)} tables")
    return StepResult.success(artifact_path=artifact, total_rows=total)
