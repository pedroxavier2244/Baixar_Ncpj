"""
Index step — recria o MV (se necessário) e faz REFRESH, depois remove _old tables.

Fluxo após o swap do load_step:
  0. As fontes obrigatórias (rf_estabelecimentos, rf_empresas) têm dado? Se não,
     FALHA SEM TOCAR NA MV — ela pode ser a única cópia do dado que ainda serve
     a API (incidente de 14/10/2026, explicado no run()).
  1. Se mv_cnpj_full não existe (foi dropado por CASCADE junto com _old do run anterior):
       CREATE MATERIALIZED VIEW ... WITH NO DATA
       REFRESH MATERIALIZED VIEW  (full rebuild, não-concorrente)
  2. Se mv_cnpj_full existe mas não está populado (primeira carga):
       REFRESH MATERIALIZED VIEW  (full rebuild, não-concorrente)
  3. Se a definição da MV ficou presa nas _old (o swap é RENAME, e a MV segue o
     OID): rebuild num nome ao lado + troca de nomes no fim, para não deixar a
     API sem MV durante as ~3h de construção.
  4. Se mv_cnpj_full existe, está populado e aponta para as tabelas certas:
       REFRESH MATERIALIZED VIEW CONCURRENTLY  (não bloqueia leitores)

  Depois, em qualquer caminho:
  5. DROP TABLE ... _old  (agora seguro: MV tem deps frescas nas tabelas novas)
  6. Limpa cache Redis (se configurado)
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
# Só o SELECT: o nome do alvo entra na hora. A MV é construída ora no nome
# definitivo (quando não há nada servindo), ora num nome ao lado, para a troca
# não tirar do ar a cópia que a API está usando (ver _rebuild_ao_lado).
_MV_SELECT = f"""
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
"""

# As duas tabelas que entram por JOIN (não LEFT JOIN): qualquer uma vazia zera a
# MV inteira. As de domínio (cnaes, municípios...) são LEFT JOIN — ficaram com 0
# linhas por meses sem que isso tirasse uma linha da MV.
_FONTES_OBRIGATORIAS = [
    f"{_D}.rf_estabelecimentos",
    f"{_D}.rf_empresas",
]

_MV = f"{_V}.mv_cnpj_full"
_MV_NOVA = f"{_V}.mv_cnpj_full_novo"
_SUF = "_novo"


def _ddl_mv(alvo: str, com_dado: bool) -> str:
    return (
        f"CREATE MATERIALIZED VIEW {alvo} AS {_MV_SELECT}"
        + ("" if com_dado else "\nWITH NO DATA")
    )


# (nome, único?, corpo) — o nome é renomeado na troca, então não pode ficar
# grudado no SQL.
_MV_INDEXES_SPEC = [
    ("idx_mv_cnpj_completo", True, "(cnpj_completo)"),
    ("idx_mv_cnpj_basico", False, "(cnpj_basico)"),
    ("idx_mv_razao_social_trgm", False, "USING gin (razao_social gin_trgm_ops)"),
    ("idx_mv_nome_fantasia_trgm", False, "USING gin (nome_fantasia gin_trgm_ops)"),
    ("idx_mv_municipio", False, "(municipio)"),
    ("idx_mv_uf", False, "(uf)"),
    ("idx_mv_cnae_fiscal", False, "(cnae_fiscal)"),
    ("idx_mv_situacao_cadastral", False, "(situacao_cadastral)"),
    ("idx_mv_opcao_simples", False, "(opcao_pelo_simples)"),
    ("idx_mv_opcao_mei", False, "(opcao_pelo_mei)"),
    ("idx_mv_municipio_descricao_trgm", False, "USING gin (municipio_descricao gin_trgm_ops)"),
]


def _sql_indices(alvo: str, sufixo: str = "") -> list[str]:
    return [
        f"CREATE {'UNIQUE ' if uniq else ''}INDEX IF NOT EXISTS {nome}{sufixo} "
        f"ON {alvo} {corpo}"
        for nome, uniq, corpo in _MV_INDEXES_SPEC
    ]


_MV_DDL = _ddl_mv(_MV, com_dado=False)
_MV_INDEXES = _sql_indices(_MV)


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


def _tem_linha(conn: psycopg.Connection, rel: str) -> bool:
    """
    Tem pelo menos uma linha? É EXISTS, não COUNT: com 70 milhões de linhas o
    COUNT desta MV levou 22 MINUTOS em 14/10/2026 (06:20 -> 06:42 no log do
    worker), e aqui a pergunta é só "tem dado ou não".
    """
    row = conn.execute(f"SELECT EXISTS (SELECT 1 FROM {rel} LIMIT 1)").fetchone()
    return bool(row and row[0])


def _fontes_vazias(conn: psycopg.Connection) -> list[str]:
    """Quais das fontes obrigatórias da MV estão vazias."""
    return [t for t in _FONTES_OBRIGATORIAS if not _tem_linha(conn, t)]


def _aponta_para_old(conn: psycopg.Connection) -> bool:
    """
    A definição da MV está presa às tabelas _old?

    A MV guarda a fonte por OID, não por nome. O swap do load_step é
    `ALTER TABLE ... RENAME`, que preserva o OID — então renomear
    rf_empresas -> rf_empresas_old ARRASTA a definição da MV junto, e um
    REFRESH passa a ler as _old em vez das tabelas recém-carregadas. Dá para
    ver em pg_matviews.definition: `FROM cnpj.rf_estabelecimentos_old est
    JOIN cnpj.rf_empresas_old e ...`.
    """
    row = conn.execute(
        "SELECT definition FROM pg_matviews "
        f"WHERE schemaname = '{_V}' AND matviewname = 'mv_cnpj_full'"
    ).fetchone()
    return bool(row and row[0] and "_old" in row[0])


def _recreate_mv(conn: psycopg.Connection) -> None:
    """Recria mv_cnpj_full do zero (após DROP por CASCADE ou primeira instalação)."""
    log.info("recreating mv_cnpj_full...")
    conn.execute(_MV_DDL)
    for idx_sql in _MV_INDEXES:
        conn.execute(idx_sql)
    log.info("mv_cnpj_full recreated (WITH NO DATA — REFRESH a seguir)")


def _rebuild_ao_lado(conn: psycopg.Connection) -> None:
    """
    Reconstrói a MV num nome ao lado e troca os nomes no fim.

    É o caminho de quando a definição ficou presa nas _old: um REFRESH leria a
    tabela errada, e o DROP + CREATE direto deixaria a API sem MV durante a
    construção inteira — que em 14/10/2026 levou 2h46 (03:34 -> 06:20), ou seja,
    entraria no horário comercial.

    A nova nasce WITH DATA. A viva só sai do ar na troca de nomes, que é uma
    transação de milissegundos, e só DEPOIS de a nova estar conferida com dado:
    trocar por uma MV vazia é o pior resultado possível, pior que não trocar.
    """
    log.info(f"construindo {_MV_NOVA} ao lado (a MV viva continua servindo)...")
    conn.execute(f"DROP MATERIALIZED VIEW IF EXISTS {_MV_NOVA}")
    conn.execute(_ddl_mv(_MV_NOVA, com_dado=True))
    for idx_sql in _sql_indices(_MV_NOVA, _SUF):
        conn.execute(idx_sql)

    if not _tem_linha(conn, _MV_NOVA):
        conn.execute(f"DROP MATERIALIZED VIEW IF EXISTS {_MV_NOVA}")
        raise RuntimeError(
            f"{_MV_NOVA} saiu vazia — troca NAO feita, a MV que esta servindo "
            "ficou intacta"
        )

    log.info("trocando os nomes (transacao curta)...")
    with conn.transaction():
        # O DROP primeiro libera os nomes dos índices, que são únicos por schema.
        conn.execute(f"DROP MATERIALIZED VIEW IF EXISTS {_MV}")
        conn.execute(f"ALTER MATERIALIZED VIEW {_MV_NOVA} RENAME TO mv_cnpj_full")
        for nome, _uniq, _corpo in _MV_INDEXES_SPEC:
            # Sem isto o unique index volta chamado idx_mv_cnpj_completo_novo, e o
            # REFRESH CONCURRENTLY do mês seguinte — que EXIGE um unique index —
            # falharia sem relação aparente com este rebuild.
            conn.execute(f"ALTER INDEX {_V}.{nome}{_SUF} RENAME TO {nome}")
    log.info("mv_cnpj_full trocada pela nova, apontando para as tabelas atuais")


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

            # ANTES DE QUALQUER REFRESH: a fonte tem dado?
            #
            # O REFRESH é o ato destrutivo — ele substitui o conteúdo da MV. A
            # conferência de linhas que existe no fim deste passo acusa o
            # estrago, não impede: em 14/10/2026 ela falhou três vezes
            # (03:34, 07:42, 08:47) com a API já respondendo "não encontrado"
            # para qualquer CNPJ desde o primeiro refresh.
            #
            # E a MV pode ser a ÚNICA cópia do dado: as rf_* são UNLOGGED, e um
            # reinício sujo do Postgres as esvazia sem tocar na MV (06/10/2026).
            # Foi essa a combinação: tabelas vazias desde o dia 6, a MV ainda
            # servindo, e o refresh da carga mensal apagou a última cópia.
            vazias = _fontes_vazias(conn)
            if vazias:
                return StepResult.failed(
                    "fontes obrigatorias da mv_cnpj_full vazias: "
                    + ", ".join(vazias)
                    + " — a MV foi preservada (segue servindo o dado anterior); "
                    "verifique o load step"
                )

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

            elif _aponta_para_old(conn):
                # O swap por rename arrastou a definição para as _old: um
                # REFRESH leria a tabela errada (e as _old o load_step esvazia /
                # dropa). Reconstrói ao lado e troca no fim.
                _apply_session_settings(conn)
                log.warning(
                    "a definicao da MV aponta para as tabelas _old (swap por "
                    "rename) — rebuild ao lado em vez de REFRESH"
                )
                _rebuild_ao_lado(conn)

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
