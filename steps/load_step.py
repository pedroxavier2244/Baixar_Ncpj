"""
Load step — incremental merge via staging tables.

Para empresas e estabelecimentos:
  1. TRUNCATE staging
  2. COPY dump completo -> staging  (rapido: UNLOGGED, sem indices de dados, sem constraints)
  3. INSERT novas linhas (presentes em staging, ausentes em main)
  4. UPDATE linhas alteradas (PK bate mas dados diferem)
  5. DELETE linhas removidas pela RF (presentes em main, ausentes em staging)
     — protegido por _check_staging_volume para evitar exclusao massiva acidental

Para socios e simples: sem PK natural estavel -> TRUNCATE main + INSERT from staging.
Para lookup tables (cnaes, municipios, ...): TRUNCATE + COPY (pequenas, rapidas).
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import psycopg

from config import settings
from logger import get_logger
from steps.base import StepResult, read_artifact, write_artifact

log = get_logger("step.load")

# ── Schemas ────────────────────────────────────────────────────────────────
_D = settings.pg_schema          # "cnpj"         — dados finais
_S = settings.pg_staging_schema  # "cnpj_staging" — tabelas UNLOGGED

# ── Definicao de colunas ──────────────────────────────────────────────────
# Deve corresponder exatamente ao header do CSV transformado.

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

_SIMPLES_COLS: list[str] = [
    "cnpj_basico", "opcao_pelo_simples", "data_opcao_simples",
    "data_exclusao_simples", "opcao_pelo_mei", "data_opcao_mei",
    "data_exclusao_mei",
]

LOOKUP_TABLE_MAP: dict[str, str] = {
    "cnaes":         f"{_D}.rf_cnaes",
    "municipios":    f"{_D}.rf_municipios",
    "naturezas":     f"{_D}.rf_naturezas",
    "qualificacoes": f"{_D}.rf_qualificacoes",
    "motivos":       f"{_D}.rf_motivos",
    "paises":        f"{_D}.rf_paises",
    "portes":        f"{_D}.rf_portes",
}


# ── Helpers de baixo nivel ─────────────────────────────────────────────────

def _read_header(csv_path: Path) -> list[str]:
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        return next(csv.reader(f))


def _copy_csv(conn: psycopg.Connection, table: str,
              csv_path: Path, columns: list[str]) -> int:
    """Stream csv_path para table via COPY. Retorna contagem aproximada de linhas."""
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


# ── Staging ────────────────────────────────────────────────────────────────

def _stage(conn: psycopg.Connection, staging_table: str,
           csv_path: Path, columns: list[str]) -> int:
    """TRUNCATE staging e COPY o dump completo."""
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {staging_table}")
    count = _copy_csv(conn, staging_table, csv_path, columns)
    log.info(f"staged ~{count:,} rows into {staging_table}")
    return count


# ── Volume guard ───────────────────────────────────────────────────────────

def _check_staging_volume(conn: psycopg.Connection,
                          staging_table: str, main_table: str,
                          min_ratio: float = 0.5) -> None:
    """
    Aborta se staging tem menos que min_ratio * linhas do main.
    Previne DELETE massivo acidental quando staging e parcial.
    Ignorado quando main esta vazio (primeira carga).
    """
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {staging_table}")
        staging_count = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM {main_table}")
        main_count = cur.fetchone()[0]

    if main_count > 0 and staging_count < main_count * min_ratio:
        raise RuntimeError(
            f"Volume check falhou: {staging_table} tem {staging_count:,} linhas "
            f"mas {main_table} tem {main_count:,}. "
            f"Staging < {int(min_ratio * 100)}% do main — abortando para evitar perda de dados."
        )
    log.info(f"volume check OK: staging={staging_count:,} main={main_count:,}")


# ── Diff / merge ───────────────────────────────────────────────────────────

def _diff_merge(conn: psycopg.Connection,
                pg_table: str, staging_table: str,
                pk_cols: list[str], data_cols: list[str],
                run_key: str) -> dict[str, int]:
    """
    Diff de tres vias entre staging (dump novo) e main (DB atual):
      - INSERT linhas em staging mas nao em main  -> novos registros
      - UPDATE linhas com PK igual mas dados diferentes -> alterados
      - DELETE linhas em main mas nao em staging  -> removidos pela RF
    Retorna {"inserted": N, "updated": N, "deleted": N}.
    """
    _check_staging_volume(conn, staging_table, pg_table)

    non_pk = [c for c in data_cols if c not in pk_cols]

    pk_join    = " AND ".join(f"t.{c} = s.{c}" for c in pk_cols)
    pk_sub_ts  = " AND ".join(f"s.{c} = t.{c}" for c in pk_cols)
    pk_sub_st  = " AND ".join(f"t.{c} = s.{c}" for c in pk_cols)
    pk_present = " AND ".join(
        f"NULLIF(BTRIM(s.{c}::text), '') IS NOT NULL" for c in pk_cols
    )

    ins_cols = ", ".join(f'"{c}"' for c in data_cols)
    ins_vals = ", ".join(f"s.{c}" for c in data_cols)
    set_clause = ", ".join(f'"{c}" = s.{c}' for c in non_pk)
    t_tuple = "(" + ", ".join(f"t.{c}" for c in non_pk) + ")"
    s_tuple = "(" + ", ".join(f"s.{c}" for c in non_pk) + ")"

    params = {"run_key": run_key}

    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO {pg_table} ({ins_cols}, run_key, created_at, updated_at)
            SELECT {ins_vals}, %(run_key)s, NOW(), NOW()
            FROM {staging_table} s
            WHERE ({pk_present})
              AND NOT EXISTS (SELECT 1 FROM {pg_table} t WHERE {pk_sub_st})
        """, params)
        inserted = cur.rowcount

    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {pg_table} t
            SET {set_clause}, run_key = %(run_key)s, updated_at = NOW()
            FROM {staging_table} s
            WHERE ({pk_present})
              AND {pk_join}
              AND {t_tuple} IS DISTINCT FROM {s_tuple}
        """, params)
        updated = cur.rowcount

    with conn.cursor() as cur:
        cur.execute(f"""
            DELETE FROM {pg_table} t
            WHERE NOT EXISTS (
                SELECT 1 FROM {staging_table} s WHERE {pk_sub_ts}
            )
        """)
        deleted = cur.rowcount

    return {"inserted": inserted, "updated": updated, "deleted": deleted}


# ── Replace strategies ─────────────────────────────────────────────────────

def _replace_socios(conn: psycopg.Connection, run_key: str) -> int:
    """Socios sem PK natural: TRUNCATE main + INSERT from staging."""
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {_D}.rf_socios RESTART IDENTITY")

    cols   = ", ".join(f'"{c}"' for c in _SOCIOS_COLS)
    s_cols = ", ".join(f"s.{c}" for c in _SOCIOS_COLS)
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO {_D}.rf_socios ({cols}, run_key, created_at, updated_at)
            SELECT {s_cols}, %(run_key)s, NOW(), NOW()
            FROM {_S}.rf_socios s
        """, {"run_key": run_key})
        return cur.rowcount


def _replace_simples(conn: psycopg.Connection, run_key: str) -> int:
    """
    Simples: TRUNCATE main + INSERT from staging.
    RF republica o arquivo completo todo mes.
    """
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {_D}.rf_simples")

    cols   = ", ".join(f'"{c}"' for c in _SIMPLES_COLS)
    s_cols = ", ".join(f"s.{c}" for c in _SIMPLES_COLS)
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO {_D}.rf_simples ({cols}, run_key, updated_at)
            SELECT {s_cols}, %(run_key)s, NOW()
            FROM {_S}.rf_simples s
            WHERE NULLIF(BTRIM(s.cnpj_basico), '') IS NOT NULL
        """, {"run_key": run_key})
        return cur.rowcount


def _load_lookup(conn: psycopg.Connection,
                 pg_table: str, csv_path: Path) -> int:
    """TRUNCATE + COPY para tabelas de lookup pequenas."""
    header = _read_header(csv_path)
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {pg_table} RESTART IDENTITY CASCADE")
    return _copy_csv(conn, pg_table, csv_path, header)


# ── Step principal ─────────────────────────────────────────────────────────

def run(job_id: str, run_key: str, checkpoint_dir: Path) -> StepResult:
    try:
        manifest = read_artifact(checkpoint_dir / "transform_manifest.json")
    except FileNotFoundError as e:
        return StepResult.failed(str(e))

    out_dir = Path(manifest["out_dir"])
    results: list[dict] = []

    try:
        with psycopg.connect(settings.postgres_url, autocommit=False) as conn:
            for table_entry in manifest.get("tables", []):
                keyword = table_entry["keyword"]
                tbl_key = keyword.lower()
                csv_path = out_dir / f"{tbl_key}.csv"

                if not csv_path.exists():
                    log.warning(f"CSV transformado nao encontrado: {csv_path} — pulando")
                    continue

                if tbl_key == "empresas":
                    _stage(conn, f"{_S}.rf_empresas", csv_path, _EMPRESAS_COLS)
                    diff = _diff_merge(
                        conn,
                        f"{_D}.rf_empresas", f"{_S}.rf_empresas",
                        _EMPRESAS_PK, _EMPRESAS_COLS, run_key,
                    )
                    conn.commit()
                    log.info(
                        f"rf_empresas — inserted {diff['inserted']:,}, "
                        f"updated {diff['updated']:,}, deleted {diff['deleted']:,}"
                    )
                    results.append({"table": f"{_D}.rf_empresas", **diff})

                elif tbl_key == "estabelecimentos":
                    _stage(conn, f"{_S}.rf_estabelecimentos", csv_path, _ESTAB_COLS)
                    diff = _diff_merge(
                        conn,
                        f"{_D}.rf_estabelecimentos", f"{_S}.rf_estabelecimentos",
                        _ESTAB_PK, _ESTAB_COLS, run_key,
                    )
                    conn.commit()
                    log.info(
                        f"rf_estabelecimentos — inserted {diff['inserted']:,}, "
                        f"updated {diff['updated']:,}, deleted {diff['deleted']:,}"
                    )
                    results.append({"table": f"{_D}.rf_estabelecimentos", **diff})

                elif tbl_key == "socios":
                    _stage(conn, f"{_S}.rf_socios", csv_path, _SOCIOS_COLS)
                    count = _replace_socios(conn, run_key)
                    conn.commit()
                    log.info(f"rf_socios — replaced with {count:,} rows")
                    results.append({
                        "table": f"{_D}.rf_socios",
                        "inserted": count, "updated": 0, "deleted": 0,
                    })

                elif tbl_key == "simples":
                    _stage(conn, f"{_S}.rf_simples", csv_path, _SIMPLES_COLS)
                    count = _replace_simples(conn, run_key)
                    conn.commit()
                    log.info(f"rf_simples — replaced with {count:,} rows")
                    results.append({
                        "table": f"{_D}.rf_simples",
                        "inserted": count, "updated": 0, "deleted": 0,
                    })

                elif tbl_key in LOOKUP_TABLE_MAP:
                    pg_table = LOOKUP_TABLE_MAP[tbl_key]
                    count = _load_lookup(conn, pg_table, csv_path)
                    conn.commit()
                    log.info(f"{pg_table} — loaded {count:,} rows")
                    results.append({"table": pg_table, "inserted": count})

                else:
                    log.warning(f"keyword '{keyword}' desconhecida — pulando")

    except Exception as exc:
        import traceback
        return StepResult.failed(f"load failed: {exc}\n{traceback.format_exc()}")

    artifact = checkpoint_dir / "load_manifest.json"
    write_artifact(artifact, {"run_key": run_key, "tables": results})

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
