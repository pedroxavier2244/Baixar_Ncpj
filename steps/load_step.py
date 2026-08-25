"""
Load step — schema swap strategy.

Para cada tabela principal (empresas, estabelecimentos, socios, simples):
  1. DROP TABLE <nome>_old CASCADE  (tabelas antigas do run anterior + deps obsoletas do MV)
  2. CREATE TABLE <nome>_new        (mesma DDL, com run_key/created_at como DEFAULT)
  3. COPY CSV direto para <nome>_new (sem índices ainda = máxima velocidade)
  4. CREATE INDEX em <nome>_new     (após carga = muito mais rápido)
  5. Swap atômico (milissegundos):
       ALTER TABLE <nome>     RENAME TO <nome>_old
       ALTER TABLE <nome>_new RENAME TO <nome>
  6. <nome>_old permanece até o index_step recriar o MV e fazer o DROP final

Para lookup tables (cnaes, municipios, etc.): TRUNCATE + COPY (pequenas, rápidas).

Vantagens vs diff-merge incremental:
  - Sem UPDATE/DELETE em tabelas live → sem bloat MVCC
  - Tabela live intocada durante build → zero impacto em queries
  - Swap atômico (metadados apenas, < 1ms)
  - Rollback em falha: _new simplesmente não é promovida
  - MV recriada no index_step após swap (deps frescas)
"""
from __future__ import annotations

import csv
from pathlib import Path

import psycopg

from config import settings
from logger import get_logger
from steps.base import StepResult, read_artifact, write_artifact

log = get_logger("step.load")

# ── Schemas ────────────────────────────────────────────────────────────────
_D = settings.pg_schema          # "cnpj"         — dados finais
_S = settings.pg_staging_schema  # "cnpj_staging" — não usado no swap, mantido por compatibilidade

# ── Definição de colunas (apenas dados — sem run_key/created_at/updated_at) ──
_EMPRESAS_COLS: list[str] = [
    "cnpj_basico", "razao_social", "natureza_juridica",
    "qualificacao_responsavel", "capital_social", "porte",
    "ente_federativo_responsavel",
]

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

# Tabelas principais que participam do swap
_MAIN_TABLES = [
    f"{_D}.rf_empresas",
    f"{_D}.rf_estabelecimentos",
    f"{_D}.rf_socios",
    f"{_D}.rf_simples",
]


# ── DDL das tabelas _new ────────────────────────────────────────────────────
# run_key e timestamps definidos como DEFAULT → preenchidos automaticamente
# pelo COPY sem precisar de UPDATE posterior (muito mais rápido em 60M linhas).

# NOTA: a PRIMARY KEY NÃO é declarada inline. Os dados da RF ocasionalmente
# trazem o mesmo cnpj_basico duas vezes (registro real + stub vazio); por isso
# o COPY entra sem PK, deduplica-se (mantendo a linha mais completa) e só então
# o load adiciona a PK. Ver _dedup_in_place / _build_new_table(pk_cols=...).
def _empresas_new_ddl(run_key: str) -> str:
    return f"""
CREATE UNLOGGED TABLE {_D}.rf_empresas_new (
    cnpj_basico                  CHAR(8)     NOT NULL,
    razao_social                 TEXT,
    natureza_juridica            CHAR(4),
    qualificacao_responsavel     CHAR(2),
    capital_social               TEXT,
    porte                        CHAR(2),
    ente_federativo_responsavel  TEXT,
    run_key                      CHAR(7)     DEFAULT '{run_key}',
    created_at                   TIMESTAMPTZ DEFAULT NOW(),
    updated_at                   TIMESTAMPTZ DEFAULT NOW()
)"""


def _estab_new_ddl(run_key: str) -> str:
    return f"""
CREATE UNLOGGED TABLE {_D}.rf_estabelecimentos_new (
    cnpj_basico                  CHAR(8)     NOT NULL,
    cnpj_ordem                   CHAR(4)     NOT NULL,
    cnpj_dv                      CHAR(2)     NOT NULL,
    identificador_matriz_filial  CHAR(1),
    nome_fantasia                TEXT,
    situacao_cadastral           CHAR(2),
    data_situacao_cadastral      CHAR(8),
    motivo_situacao_cadastral    CHAR(2),
    nm_cidade_exterior           TEXT,
    pais                         CHAR(3),
    data_inicio_atividade        CHAR(8),
    cnae_fiscal                  CHAR(7),
    cnae_fiscal_secundaria       TEXT,
    tipo_logradouro              TEXT,
    logradouro                   TEXT,
    numero                       TEXT,
    complemento                  TEXT,
    bairro                       TEXT,
    cep                          CHAR(8),
    uf                           CHAR(2),
    municipio                    CHAR(7),
    ddd1                         TEXT,
    telefone1                    TEXT,
    ddd2                         TEXT,
    telefone2                    TEXT,
    ddd_fax                      TEXT,
    fax                          TEXT,
    correio_eletronico           TEXT,
    situacao_especial            TEXT,
    data_situacao_especial       CHAR(8),
    run_key                      CHAR(7)     DEFAULT '{run_key}',
    created_at                   TIMESTAMPTZ DEFAULT NOW(),
    updated_at                   TIMESTAMPTZ DEFAULT NOW()
)"""  # PK composta adicionada após o dedup — ver _build_new_table(pk_cols=...)


def _socios_new_ddl(run_key: str) -> str:
    # id GENERATED ALWAYS AS IDENTITY: preenchido automaticamente durante COPY
    # (id não está em _SOCIOS_COLS, então o COPY especifica apenas as colunas de dados)
    return f"""
CREATE UNLOGGED TABLE {_D}.rf_socios_new (
    id                           BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cnpj_basico                  CHAR(8),
    identificador_socio          CHAR(1),
    nome_socio                   TEXT,
    cnpj_cpf_socio               TEXT,
    qualificacao_socio           CHAR(2),
    data_entrada_sociedade       CHAR(8),
    pais                         CHAR(3),
    representante_legal          TEXT,
    nome_representante           TEXT,
    qualificacao_representante   CHAR(2),
    faixa_etaria                 CHAR(1),
    run_key                      CHAR(7)     DEFAULT '{run_key}',
    created_at                   TIMESTAMPTZ DEFAULT NOW(),
    updated_at                   TIMESTAMPTZ DEFAULT NOW()
)"""


def _simples_new_ddl(run_key: str) -> str:
    return f"""
CREATE UNLOGGED TABLE {_D}.rf_simples_new (
    cnpj_basico           CHAR(8)     NOT NULL,
    opcao_pelo_simples    CHAR(1),
    data_opcao_simples    CHAR(8),
    data_exclusao_simples CHAR(8),
    opcao_pelo_mei        CHAR(1),
    data_opcao_mei        CHAR(8),
    data_exclusao_mei     CHAR(8),
    run_key               CHAR(7)     DEFAULT '{run_key}',
    updated_at            TIMESTAMPTZ DEFAULT NOW()
)"""  # PK adicionada após o dedup — ver _build_new_table(pk_cols=...)


# ── Helpers de baixo nível ──────────────────────────────────────────────────

def _read_header(csv_path: Path) -> list[str]:
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        return next(csv.reader(f))


def _copy_csv(conn: psycopg.Connection, table: str,
              csv_path: Path, columns: list[str]) -> int:
    """Streama csv_path para table via COPY. Retorna contagem aproximada de linhas."""
    col_sql = ", ".join(f'"{c}"' for c in columns)
    sql = f'COPY {table} ({col_sql}) FROM STDIN WITH (FORMAT csv, HEADER true)'
    rows = 0
    with conn.cursor() as cur:
        with cur.copy(sql) as copy:
            with open(csv_path, "r", encoding="utf-8") as f:
                while chunk := f.read(65536):
                    chunk = chunk.replace('\x00', '')  # remove null bytes rejeitados pelo Postgres
                    copy.write(chunk)
                    rows += chunk.count("\n")
    return max(0, rows - 1)


# ── Limpeza de runs anteriores ──────────────────────────────────────────────

def _drop_previous_old_tables(conn: psycopg.Connection) -> None:
    """
    Remove as tabelas _old deixadas pelo swap do run anterior.
    CASCADE é necessário porque o MV em cnpj_serving ainda pode ter
    dependência de OID nas tabelas antigas — o index_step recria o MV.
    """
    dropped = []
    for live in _MAIN_TABLES:
        schema, tname = live.rsplit(".", 1)
        old = f"{schema}.{tname}_old"
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema=%s AND table_name=%s",
                (schema, f"{tname}_old"),
            )
            if cur.fetchone():
                cur.execute(f"DROP TABLE {old} CASCADE")
                dropped.append(old)
    conn.commit()
    if dropped:
        log.info(f"dropped previous _old tables (CASCADE): {dropped}")
    else:
        log.info("no _old tables from previous run found")


# ── Build + swap ────────────────────────────────────────────────────────────

def _dedup_in_place(
    conn: psycopg.Connection,
    table: str,
    key_cols: list[str],
    data_cols: list[str],
) -> int:
    """
    Remove duplicatas pela chave natural, mantendo a linha mais completa.

    Os arquivos da RF ocasionalmente trazem o mesmo cnpj_basico duas vezes —
    tipicamente um registro real + um stub vazio. Sem dedup, o ADD PRIMARY KEY
    seguinte falharia com UniqueViolation.

    Critério de desempate: mantém a linha com mais colunas de dados preenchidas
    (não-nulas); em empate, a primeira fisicamente (menor ctid).
    Retorna a quantidade de linhas removidas.
    """
    key_list = ", ".join(key_cols)
    completeness = " + ".join(f"({c} IS NOT NULL)::int" for c in data_cols) or "0"
    sql = f"""
        DELETE FROM {table} t
        USING (
            SELECT ctid,
                   row_number() OVER (
                       PARTITION BY {key_list}
                       ORDER BY ({completeness}) DESC, ctid
                   ) AS rn
            FROM {table}
        ) d
        WHERE t.ctid = d.ctid AND d.rn > 1
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        removed = cur.rowcount
    conn.commit()
    if removed and removed > 0:
        log.warning(
            f"  {table}: {removed:,} linha(s) duplicada(s) por chave natural "
            f"removida(s) (dado duplicado na fonte RF)"
        )
    return max(0, removed or 0)


def _build_new_table(
    conn: psycopg.Connection,
    new_table: str,
    create_ddl: str,
    csv_path: Path,
    columns: list[str],
    index_sqls: list[str],
    pk_cols: list[str] | None = None,
) -> int:
    """
    Cria <nome>_new do zero, COPY do CSV, cria índices.
    Retorna contagem aproximada de linhas.

    Se pk_cols for informado, a tabela é criada SEM primary key inline; após o
    COPY, deduplica-se pela chave natural (mantendo a linha mais completa) e só
    então adiciona-se a PRIMARY KEY. Isso torna o load resiliente a cnpj_basico
    duplicado na fonte da RF. Tabelas com PK sintética (socios) passam pk_cols=None.
    """
    # Remove tentativa anterior com falha
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {new_table}")
    conn.commit()

    log.info(f"creating {new_table}...")
    with conn.cursor() as cur:
        cur.execute(create_ddl)
    conn.commit()

    count = _copy_csv(conn, new_table, csv_path, columns)
    conn.commit()
    log.info(f"  {new_table}: ~{count:,} rows loaded")

    # Dedup pela chave natural + ADD PRIMARY KEY (antes dos índices secundários).
    if pk_cols:
        data_cols = [c for c in columns if c not in pk_cols]
        _dedup_in_place(conn, new_table, pk_cols, data_cols)
        key_list = ", ".join(pk_cols)
        with conn.cursor() as cur:
            cur.execute(f"ALTER TABLE {new_table} ADD PRIMARY KEY ({key_list})")
        conn.commit()
        log.info(f"  {new_table}: PRIMARY KEY ({key_list}) criada após dedup")

    # Índices secundários criados APÓS a carga = drasticamente mais rápido
    # Drop pelo nome antes de criar — o swap renomeia a tabela mas mantém o nome do índice,
    # então tentativas seguintes encontrariam o índice "órfão" na tabela live.
    for idx_sql in index_sqls:
        parts = idx_sql.split()
        idx_name = parts[2]  # "CREATE INDEX idx_name ON ..."
        schema, _ = new_table.rsplit(".", 1)
        with conn.cursor() as cur:
            cur.execute(f"DROP INDEX IF EXISTS {schema}.{idx_name}")
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(idx_sql)
        conn.commit()
    log.info(f"  {new_table}: indexes ready")

    return count


def _swap_table(conn: psycopg.Connection, live_table: str) -> None:
    """
    Swap atômico: live → _old, _new → live.
    Executa em uma única transação — operação de metadados, < 1ms.
    A tabela live permanece disponível para queries durante todo o build.
    """
    schema, tname = live_table.rsplit(".", 1)
    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE {schema}.{tname}     RENAME TO {tname}_old")
        cur.execute(f"ALTER TABLE {schema}.{tname}_new RENAME TO {tname}")
    conn.commit()
    log.info(f"swap OK: {live_table}  (_old mantida até index_step recriar MV)")


# ── Lookup ──────────────────────────────────────────────────────────────────

def _load_lookup(conn: psycopg.Connection, pg_table: str, csv_path: Path) -> int:
    """TRUNCATE + COPY para tabelas de domínio pequenas."""
    header = _read_header(csv_path)
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {pg_table} RESTART IDENTITY CASCADE")
    count = _copy_csv(conn, pg_table, csv_path, header)
    conn.commit()
    return count


# ── Limpeza pós-swap ────────────────────────────────────────────────────────

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


# ── Step principal ──────────────────────────────────────────────────────────

def run(job_id: str, run_key: str, checkpoint_dir: Path) -> StepResult:
    try:
        manifest = read_artifact(checkpoint_dir / "transform_manifest.json")
    except FileNotFoundError as e:
        return StepResult.failed(str(e))

    out_dir = Path(manifest["out_dir"])
    results: list[dict] = []

    try:
        with psycopg.connect(settings.postgres_url, autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute("SET work_mem = '32MB'")
                cur.execute("SET maintenance_work_mem = '256MB'")
            conn.commit()

            # Passo 0: limpar _old do run anterior (CASCADE remove MV obsoleta)
            _drop_previous_old_tables(conn)

            for table_entry in manifest.get("tables", []):
                keyword = table_entry["keyword"]
                tbl_key = keyword.lower()
                csv_path = out_dir / f"{tbl_key}.csv"

                if not csv_path.exists():
                    log.warning(f"CSV transformado não encontrado: {csv_path} — pulando")
                    continue

                if tbl_key == "empresas":
                    # Sem índice secundário: a PRIMARY KEY (cnpj_basico) já indexa a chave.
                    count = _build_new_table(
                        conn,
                        new_table=f"{_D}.rf_empresas_new",
                        create_ddl=_empresas_new_ddl(run_key),
                        csv_path=csv_path,
                        columns=_EMPRESAS_COLS,
                        index_sqls=[],
                        pk_cols=["cnpj_basico"],
                    )
                    _swap_table(conn, f"{_D}.rf_empresas")
                    results.append({"table": f"{_D}.rf_empresas", "inserted": count})
                    log.info(f"rf_empresas swapped — {count:,} rows")

                elif tbl_key == "estabelecimentos":
                    # A PK composta já indexa (cnpj_basico, cnpj_ordem, cnpj_dv);
                    # mantém-se apenas o índice por cnpj_basico isolado (lookups por raiz).
                    count = _build_new_table(
                        conn,
                        new_table=f"{_D}.rf_estabelecimentos_new",
                        create_ddl=_estab_new_ddl(run_key),
                        csv_path=csv_path,
                        columns=_ESTAB_COLS,
                        index_sqls=[
                            f"CREATE INDEX idx_rf_estab_new_cnpj_basico "
                            f"ON {_D}.rf_estabelecimentos_new (cnpj_basico)",
                        ],
                        pk_cols=["cnpj_basico", "cnpj_ordem", "cnpj_dv"],
                    )
                    _swap_table(conn, f"{_D}.rf_estabelecimentos")
                    results.append({"table": f"{_D}.rf_estabelecimentos", "inserted": count})
                    log.info(f"rf_estabelecimentos swapped — {count:,} rows")

                elif tbl_key == "socios":
                    # id (GENERATED ALWAYS AS IDENTITY) é preenchido automaticamente
                    # durante COPY porque não está listado em _SOCIOS_COLS
                    count = _build_new_table(
                        conn,
                        new_table=f"{_D}.rf_socios_new",
                        create_ddl=_socios_new_ddl(run_key),
                        csv_path=csv_path,
                        columns=_SOCIOS_COLS,
                        index_sqls=[
                            f"CREATE INDEX idx_rf_socios_new_cnpj_basico "
                            f"ON {_D}.rf_socios_new (cnpj_basico)",
                            # Busca reversa (pessoa -> empresas), que o /socios/buscar faz:
                            # filtra por (cnpj_cpf_socio, nome_socio). Sem este indice o
                            # Postgres varre as 28M linhas em seq scan — 3,1s medidos na VPS,
                            # contra statement_timeout de 8s da API: passa hoje e estoura
                            # assim que o banco pega carga.
                            #
                            # Ele JA tinha sido criado pela migration 006, mas a mao e na
                            # tabela viva — e e exatamente isso que o swap mensal desfaz:
                            # quem sobrevive ao rename e o indice criado aqui, na _new. Por
                            # isso foi encontrado AUSENTE em 24/08/2026 (run_key 2026-08),
                            # com a migration 006 versionada e aparentemente aplicada.
                            # Mesma armadilha do cabecalho da migration 008 para colunas:
                            # o que nao passa por index_sqls nao sobrevive a carga.
                            f"CREATE INDEX idx_rf_socios_new_cpf_nome "
                            f"ON {_D}.rf_socios_new (cnpj_cpf_socio, nome_socio)",
                        ],
                    )
                    _swap_table(conn, f"{_D}.rf_socios")
                    results.append({"table": f"{_D}.rf_socios", "inserted": count})
                    log.info(f"rf_socios swapped — {count:,} rows")

                elif tbl_key == "simples":
                    # Sem índice secundário: a PRIMARY KEY (cnpj_basico) já indexa a chave.
                    count = _build_new_table(
                        conn,
                        new_table=f"{_D}.rf_simples_new",
                        create_ddl=_simples_new_ddl(run_key),
                        csv_path=csv_path,
                        columns=_SIMPLES_COLS,
                        index_sqls=[],
                        pk_cols=["cnpj_basico"],
                    )
                    _swap_table(conn, f"{_D}.rf_simples")
                    results.append({"table": f"{_D}.rf_simples", "inserted": count})
                    log.info(f"rf_simples swapped — {count:,} rows")

                elif tbl_key in LOOKUP_TABLE_MAP:
                    pg_table = LOOKUP_TABLE_MAP[tbl_key]
                    count = _load_lookup(conn, pg_table, csv_path)
                    log.info(f"{pg_table} — {count:,} rows loaded")
                    results.append({"table": pg_table, "inserted": count})

                else:
                    log.warning(f"keyword '{keyword}' desconhecida — pulando")

    except Exception as exc:
        import traceback
        return StepResult.failed(f"load failed: {exc}\n{traceback.format_exc()}")

    # Libera espaço em disco antes do index_step
    _cleanup_etl_files(run_key)

    artifact = checkpoint_dir / "load_manifest.json"
    write_artifact(artifact, {"run_key": run_key, "tables": results})

    total = sum(r.get("inserted", 0) for r in results)
    log.info(f"load complete — {total:,} rows across {len(results)} tables")
    return StepResult.success(artifact_path=artifact, inserted=total, updated=0, deleted=0)
