"""
Load step — incremental merge via staging tables.

For each data table (empresas, estabelecimentos):
  1. TRUNCATE staging table
  2. COPY full monthly dump → staging  (fast: unlogged, no indexes, no constraints)
  3. INSERT rows present in staging but absent from main  (new CNPJs/establishments)
  4. UPDATE rows where PK matches but data changed       (address, status, CNAE, etc.)
  5. DELETE rows present in main but absent from staging (cancelled/removed by RF)

For socios: no stable row PK → TRUNCATE main + INSERT from staging.
For lookup tables (cnaes, municipios, …): TRUNCATE + COPY (small, fast).
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import psycopg

from config import settings
from logger import get_logger
from steps.base import StepResult

log = get_logger("step.load")

# ── Column definitions ────────────────────────────────────────────────────
# Must match the transformed CSV header exactly.

_EMPRESAS_COLS: list[str] = [
    "cnpj_basico", "razao_social", "natureza_juridica",
    "qualificacao_responsavel", "capital_social", "porte",
    "ente_federativo_responsavel",
]
_EMPRESAS_PK: list[str] = ["cnpj_basico"]

_ESTAB_COLS: list[str] = [
    "cnpj_basico", "cnpj_ordem", "cnpj_dv",
    "identificador_matriz_filial", "nome_fantasia",
    "situacao_cadastral", "data_situacao_cadastral", "motivo_situacao_cadastral",
    "nm_cidade_exterior", "pais", "data_inicio_atividade",
    "cnae_fiscal", "cnae_fiscal_secundaria",
    "tipo_logradouro", "logradouro", "numero", "complemento", "bairro",
    "cep", "uf", "municipio",
    "ddd1", "telefone1", "ddd2", "telefone2", "ddd_fax", "fax",
    "correio_eletronico", "situacao_especial", "data_situacao_especial",
]
_ESTAB_PK: list[str] = ["cnpj_basico", "cnpj_ordem", "cnpj_dv"]

_SOCIOS_COLS: list[str] = [
    "cnpj_basico", "identificador_socio", "nome_socio", "cnpj_cpf_socio",
    "qualificacao_socio", "data_entrada_sociedade", "pais",
    "representante_legal", "nome_representante",
    "qualificacao_representante", "faixa_etaria",
]

LOOKUP_TABLE_MAP: dict[str, str] = {
    "cnaes":         "cnpj.rf_cnaes",
    "municipios":    "cnpj.rf_municipios",
    "naturezas":     "cnpj.rf_naturezas",
    "qualificacoes": "cnpj.rf_qualificacoes",
    "motivos":       "cnpj.rf_motivos",
    "paises":        "cnpj.rf_paises",
    "portes":        "cnpj.rf_portes",
}


# ── Low-level helpers ─────────────────────────────────────────────────────

def _read_header(csv_path: Path) -> list[str]:
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        return next(csv.reader(f))


def _copy_csv(conn: psycopg.Connection, table: str,
              csv_path: Path, columns: list[str]) -> int:
    """Stream csv_path into table via COPY. Returns approximate row count."""
    col_sql = ", ".join(f'"{c}"' for c in columns)
    sql = f'COPY {table} ({col_sql}) FROM STDIN WITH (FORMAT csv, HEADER true)'
    rows = 0
    with conn.cursor() as cur:
        with cur.copy(sql) as copy:
            with open(csv_path, "r", encoding="utf-8") as f:
                while chunk := f.read(65536):
                    copy.write(chunk)
                    rows += chunk.count("\n")
    return max(0, rows - 1)


# ── Staging helpers ───────────────────────────────────────────────────────

def _stage(conn: psycopg.Connection, staging_table: str,
           csv_path: Path, columns: list[str]) -> int:
    """TRUNCATE staging and COPY the full dump CSV into it."""
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {staging_table}")
    count = _copy_csv(conn, staging_table, csv_path, columns)
    log.info(f"staged ~{count:,} rows into {staging_table}")
    return count


# ── Diff / merge ──────────────────────────────────────────────────────────

def _diff_merge(conn: psycopg.Connection,
                pg_table: str, staging_table: str,
                pk_cols: list[str], data_cols: list[str],
                run_key: str) -> dict[str, int]:
    """
    Three-way diff between staging (new dump) and main table (current DB):
      - INSERT rows that exist in staging but not in main  → new records
      - UPDATE rows where PK matches but data differs      → changed records
      - DELETE rows that exist in main but not in staging  → removed by RF
    Returns {"inserted": N, "updated": N, "deleted": N}.
    """
    non_pk = [c for c in data_cols if c not in pk_cols]

    # Shared SQL fragments
    pk_join   = " AND ".join(f"t.{c} = s.{c}" for c in pk_cols)
    pk_sub_ts = " AND ".join(f"s.{c} = t.{c}" for c in pk_cols)  # subquery t→s
    pk_sub_st = " AND ".join(f"t.{c} = s.{c}" for c in pk_cols)  # subquery s→t

    ins_cols = ", ".join(f'"{c}"' for c in data_cols)
    ins_vals = ", ".join(f"s.{c}" for c in data_cols)

    set_clause = ", ".join(f'"{c}" = s.{c}' for c in non_pk)

    t_tuple = "(" + ", ".join(f"t.{c}" for c in non_pk) + ")"
    s_tuple = "(" + ", ".join(f"s.{c}" for c in non_pk) + ")"

    params = {"run_key": run_key}

    # 1. INSERT new rows
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO {pg_table} ({ins_cols}, run_key, created_at, updated_at)
            SELECT {ins_vals}, %(run_key)s, NOW(), NOW()
            FROM {staging_table} s
            WHERE NOT EXISTS (
                SELECT 1 FROM {pg_table} t WHERE {pk_sub_st}
            )
        """, params)
        inserted = cur.rowcount

    # 2. UPDATE changed rows (only when data actually differs)
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {pg_table} t
            SET {set_clause}, run_key = %(run_key)s, updated_at = NOW()
            FROM {staging_table} s
            WHERE {pk_join}
              AND {t_tuple} IS DISTINCT FROM {s_tuple}
        """, params)
        updated = cur.rowcount

    # 3. DELETE rows no longer in the RF dump
    with conn.cursor() as cur:
        cur.execute(f"""
            DELETE FROM {pg_table} t
            WHERE NOT EXISTS (
                SELECT 1 FROM {staging_table} s WHERE {pk_sub_ts}
            )
        """)
        deleted = cur.rowcount

    return {"inserted": inserted, "updated": updated, "deleted": deleted}


def _replace_socios(conn: psycopg.Connection, run_key: str) -> int:
    """
    Socios has no stable row PK → TRUNCATE main + INSERT from staging.
    Returns inserted row count.
    """
    with conn.cursor() as cur:
        cur.execute("TRUNCATE cnpj.rf_socios RESTART IDENTITY")

    cols = ", ".join(f'"{c}"' for c in _SOCIOS_COLS)
    s_cols = ", ".join(f"s.{c}" for c in _SOCIOS_COLS)
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO cnpj.rf_socios ({cols}, run_key, created_at, updated_at)
            SELECT {s_cols}, %(run_key)s, NOW(), NOW()
            FROM cnpj.rf_socios_staging s
        """, {"run_key": run_key})
        return cur.rowcount


def _load_lookup(conn: psycopg.Connection,
                 pg_table: str, csv_path: Path) -> int:
    """TRUNCATE + COPY for small lookup tables (cnaes, municipios, etc.)."""
    header = _read_header(csv_path)
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {pg_table} RESTART IDENTITY CASCADE")
    return _copy_csv(conn, pg_table, csv_path, header)


# ── Main step ─────────────────────────────────────────────────────────────

def run(job_id: str, run_key: str, checkpoint_dir: Path) -> StepResult:
    transform_artifact = checkpoint_dir / "transform_manifest.json"
    if not transform_artifact.exists():
        return StepResult.failed(
            "transform_manifest.json missing — run transform step first"
        )

    manifest = json.loads(transform_artifact.read_text(encoding="utf-8"))
    out_dir = Path(manifest["out_dir"])

    results: list[dict] = []

    try:
        with psycopg.connect(settings.postgres_url, autocommit=False) as conn:
            for table_entry in manifest.get("tables", []):
                keyword = table_entry["keyword"]
                tbl_key = keyword.lower()
                csv_path = out_dir / f"{tbl_key}.csv"

                if not csv_path.exists():
                    log.warning(f"transformed CSV not found: {csv_path} — skipping")
                    continue

                if tbl_key == "empresas":
                    _stage(conn, "cnpj.rf_empresas_staging", csv_path, _EMPRESAS_COLS)
                    diff = _diff_merge(
                        conn,
                        "cnpj.rf_empresas", "cnpj.rf_empresas_staging",
                        _EMPRESAS_PK, _EMPRESAS_COLS, run_key,
                    )
                    conn.commit()
                    log.info(
                        f"rf_empresas — inserted {diff['inserted']:,}, "
                        f"updated {diff['updated']:,}, deleted {diff['deleted']:,}"
                    )
                    results.append({"table": "cnpj.rf_empresas", **diff})

                elif tbl_key == "estabelecimentos":
                    _stage(conn, "cnpj.rf_estabelecimentos_staging", csv_path, _ESTAB_COLS)
                    diff = _diff_merge(
                        conn,
                        "cnpj.rf_estabelecimentos", "cnpj.rf_estabelecimentos_staging",
                        _ESTAB_PK, _ESTAB_COLS, run_key,
                    )
                    conn.commit()
                    log.info(
                        f"rf_estabelecimentos — inserted {diff['inserted']:,}, "
                        f"updated {diff['updated']:,}, deleted {diff['deleted']:,}"
                    )
                    results.append({"table": "cnpj.rf_estabelecimentos", **diff})

                elif tbl_key == "socios":
                    _stage(conn, "cnpj.rf_socios_staging", csv_path, _SOCIOS_COLS)
                    count = _replace_socios(conn, run_key)
                    conn.commit()
                    log.info(f"rf_socios — replaced with {count:,} rows")
                    results.append({
                        "table": "cnpj.rf_socios",
                        "inserted": count, "updated": 0, "deleted": 0,
                    })

                elif tbl_key in LOOKUP_TABLE_MAP:
                    pg_table = LOOKUP_TABLE_MAP[tbl_key]
                    count = _load_lookup(conn, pg_table, csv_path)
                    conn.commit()
                    log.info(f"{pg_table} — loaded {count:,} rows")
                    results.append({"table": pg_table, "inserted": count})

                else:
                    log.warning(f"unknown table keyword '{keyword}' — skipping")

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

    total_inserted = sum(r.get("inserted", 0) for r in results)
    total_updated  = sum(r.get("updated",  0) for r in results)
    total_deleted  = sum(r.get("deleted",  0) for r in results)
    log.info(
        f"load complete — inserted {total_inserted:,}, "
        f"updated {total_updated:,}, deleted {total_deleted:,}"
    )
    return StepResult.success(
        artifact_path=artifact,
        inserted=total_inserted,
        updated=total_updated,
        deleted=total_deleted,
    )
