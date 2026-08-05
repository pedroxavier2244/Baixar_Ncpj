"""
Index step — recria o MV (se necessário) e faz REFRESH, depois remove _old tables.

Fluxo após o swap do load_step:
  1. Se mv_cnpj_full não existe (foi dropado por CASCADE junto com _old do run anterior):
       CREATE MATERIALIZED VIEW ... WITH NO DATA
       REFRESH MATERIALIZED VIEW  (full rebuild, não-concorrente)
  2. Se mv_cnpj_full existe mas não está populado (primeira carga):
       REFRESH MATERIALIZED VIEW  (full rebuild, não-concorrente)
  3. Se mv_cnpj_full existe e está populado:
       REFRESH MATERIALIZED VIEW CONCURRENTLY  (não bloqueia leitores)

  Após o REFRESH:
  4. DROP TABLE ... _old  (agora seguro: MV tem deps frescas nas tabelas novas)
  5. Limpa cache Redis (se configurado)
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import psycopg

from config import settings
from logger import get_logger
from steps.base import StepResult

log = get_logger("step.index")

_D = settings.pg_schema          # "cnpj"
_V = settings.pg_serving_schema  # "cnpj_serving"

# Tabelas principais que participam do swap (para limpeza das _old)
_MAIN_TABLES = [
    f"{_D}.rf_empresas",
    f"{_D}.rf_estabelecimentos",
    f"{_D}.rf_socios",
    f"{_D}.rf_simples",
]

# ── DDL do MV (espelho do db/schema.sql — mantido em sync manualmente) ──────
_MV_DDL = f"""
CREATE MATERIALIZED VIEW {_V}.mv_cnpj_full AS
SELECT
    e.cnpj_basico || est.cnpj_ordem || est.cnpj_dv   AS cnpj_completo,
    e.cnpj_basico,
    est.cnpj_ordem,
    est.cnpj_dv,
    e.razao_social,
    e.natureza_juridica,
    nat.descricao                                      AS natureza_juridica_descricao,
    e.porte,
    prt.descricao                                      AS porte_descricao,
    e.capital_social,
    e.qualificacao_responsavel,
    e.ente_federativo_responsavel,
    est.nome_fantasia,
    est.identificador_matriz_filial,
    est.situacao_cadastral,
    mot.descricao                                      AS motivo_situacao_descricao,
    est.data_situacao_cadastral,
    est.data_inicio_atividade,
    est.cnae_fiscal,
    cnae.descricao                                     AS cnae_fiscal_descricao,
    est.cnae_fiscal_secundaria,
    est.tipo_logradouro,
    est.logradouro,
    est.numero,
    est.complemento,
    est.bairro,
    est.cep,
    est.uf,
    est.municipio,
    mun.descricao                                      AS municipio_descricao,
    est.nm_cidade_exterior,
    est.pais,
    est.ddd1,
    est.telefone1,
    est.ddd2,
    est.telefone2,
    est.ddd_fax,
    est.fax,
    est.correio_eletronico,
    est.situacao_especial,
    est.data_situacao_especial,
    sim.opcao_pelo_simples,
    sim.data_opcao_simples,
    sim.data_exclusao_simples,
    sim.opcao_pelo_mei,
    sim.data_opcao_mei,
    sim.data_exclusao_mei,
    e.run_key,
    e.updated_at
FROM {_D}.rf_estabelecimentos  est
JOIN  {_D}.rf_empresas          e    ON e.cnpj_basico   = est.cnpj_basico
LEFT JOIN {_D}.rf_cnaes         cnae ON cnae.codigo      = est.cnae_fiscal
LEFT JOIN {_D}.rf_municipios    mun  ON mun.codigo       = est.municipio
LEFT JOIN {_D}.rf_naturezas     nat  ON nat.codigo       = e.natureza_juridica
LEFT JOIN {_D}.rf_motivos       mot  ON mot.codigo       = est.motivo_situacao_cadastral
LEFT JOIN {_D}.rf_portes        prt  ON prt.codigo       = e.porte
LEFT JOIN {_D}.rf_simples       sim  ON sim.cnpj_basico  = e.cnpj_basico
WITH NO DATA
"""

_MV_INDEXES = [
    f"CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_cnpj_completo "
    f"ON {_V}.mv_cnpj_full (cnpj_completo)",

    f"CREATE INDEX IF NOT EXISTS idx_mv_cnpj_basico "
    f"ON {_V}.mv_cnpj_full (cnpj_basico)",

    f"CREATE INDEX IF NOT EXISTS idx_mv_razao_social_trgm "
    f"ON {_V}.mv_cnpj_full USING gin (razao_social gin_trgm_ops)",

    f"CREATE INDEX IF NOT EXISTS idx_mv_nome_fantasia_trgm "
    f"ON {_V}.mv_cnpj_full USING gin (nome_fantasia gin_trgm_ops)",

    f"CREATE INDEX IF NOT EXISTS idx_mv_municipio "
    f"ON {_V}.mv_cnpj_full (municipio)",

    f"CREATE INDEX IF NOT EXISTS idx_mv_uf "
    f"ON {_V}.mv_cnpj_full (uf)",

    f"CREATE INDEX IF NOT EXISTS idx_mv_cnae_fiscal "
    f"ON {_V}.mv_cnpj_full (cnae_fiscal)",

    f"CREATE INDEX IF NOT EXISTS idx_mv_situacao_cadastral "
    f"ON {_V}.mv_cnpj_full (situacao_cadastral)",

    f"CREATE INDEX IF NOT EXISTS idx_mv_opcao_simples "
    f"ON {_V}.mv_cnpj_full (opcao_pelo_simples)",

    f"CREATE INDEX IF NOT EXISTS idx_mv_opcao_mei "
    f"ON {_V}.mv_cnpj_full (opcao_pelo_mei)",

    f"CREATE INDEX IF NOT EXISTS idx_mv_municipio_descricao_trgm "
    f"ON {_V}.mv_cnpj_full USING gin (municipio_descricao gin_trgm_ops)",
]


# ── Helpers ─────────────────────────────────────────────────────────────────

def _mv_status(conn: psycopg.Connection) -> str:
    """Retorna 'missing' | 'empty' | 'populated'."""
    row = conn.execute(
        "SELECT ispopulated FROM pg_matviews "
        f"WHERE schemaname = '{_V}' AND matviewname = 'mv_cnpj_full'"
    ).fetchone()
    if row is None:
        return "missing"
    return "populated" if row[0] else "empty"


def _recreate_mv(conn: psycopg.Connection) -> None:
    """Recria mv_cnpj_full do zero (após DROP por CASCADE ou primeira instalação)."""
    log.info("recreating mv_cnpj_full...")
    conn.execute(_MV_DDL)
    for idx_sql in _MV_INDEXES:
        conn.execute(idx_sql)
    log.info("mv_cnpj_full recreated (WITH NO DATA — REFRESH a seguir)")


def _drop_old_tables(conn: psycopg.Connection) -> None:
    """
    Remove as tabelas _old deixadas pelo swap do load_step.
    Seguro após o REFRESH porque o MV agora tem deps nas tabelas novas.
    Sem CASCADE — se falhar, significa que algo inesperado depende da _old.
    """
    dropped = []
    for live in _MAIN_TABLES:
        schema, tname = live.rsplit(".", 1)
        old = f"{schema}.{tname}_old"
        row = conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema=%s AND table_name=%s",
            (schema, f"{tname}_old"),
        ).fetchone()
        if row:
            try:
                conn.execute(f"DROP TABLE {old}")
                dropped.append(old)
            except Exception as exc:
                log.warning(f"could not drop {old}: {exc} — será removida no próximo run")
    if dropped:
        log.info(f"dropped _old tables: {dropped}")
    else:
        log.info("no _old tables to drop")


def _flush_redis_cache() -> None:
    """Limpa o cache Redis após o ETL (não crítico)."""
    try:
        import redis
        r = redis.from_url(settings.redis_url)
        flushed = r.flushdb()
        log.info(f"Redis cache limpo: {flushed}")
    except Exception as exc:
        log.warning(f"Redis flush ignorado: {exc}")


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


# ── Step principal ───────────────────────────────────────────────────────────

def run(job_id: str, run_key: str, checkpoint_dir: Path) -> StepResult:
    try:
        with psycopg.connect(settings.postgres_url, autocommit=True) as conn:

            status = _mv_status(conn)
            log.info(f"mv_cnpj_full status: {status}")

            if status == "missing":
                # MV foi dropada pelo CASCADE no load_step (dep em tabela _old)
                # ou é a primeira execução após setup.py sem schema.sql aplicado
                _recreate_mv(conn)
                _apply_session_settings(conn)
                log.info("refreshing mv_cnpj_full (full rebuild após recriação)...")
                conn.execute(f"REFRESH MATERIALIZED VIEW {_V}.mv_cnpj_full")

            elif status == "empty":
                # Primeira carga real — MV existe mas nunca foi populada
                _apply_session_settings(conn)
                log.info("first-ever refresh of mv_cnpj_full (non-concurrent)...")
                conn.execute(f"REFRESH MATERIALIZED VIEW {_V}.mv_cnpj_full")

            else:
                # Atualização normal — aplica settings de sessão e verifica disco
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

            log.info("refresh complete — counting rows...")
            row = conn.execute(f"SELECT COUNT(*) FROM {_V}.mv_cnpj_full").fetchone()
            count = row[0] if row else 0
            log.info(f"mv_cnpj_full: {count:,} rows")

            if count == 0:
                return StepResult.failed(
                    "mv_cnpj_full está vazia após refresh — verifique o load step"
                )

            # Após REFRESH o MV tem deps nas tabelas novas → _old pode ser dropada com segurança
            _drop_old_tables(conn)

    except Exception as exc:
        import traceback
        return StepResult.failed(f"index step failed: {exc}\n{traceback.format_exc()}")

    _flush_redis_cache()

    artifact = checkpoint_dir / "index_manifest.json"
    tmp = artifact.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"run_key": run_key, "mv_rows": count}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(artifact)

    return StepResult.success(artifact_path=artifact, mv_rows=count)
