"""
test_e2e_schema_swap.py — integration tests for the schema-swap ETL strategy.

All tests require a real PostgreSQL instance.  Set TEST_POSTGRES_URL (or POSTGRES_URL)
in the environment before running:

    TEST_POSTGRES_URL=postgresql://user:pass@localhost:5432/testdb pytest -m integration

Each test uses the test_db fixture which creates isolated schemas with a unique suffix
and tears them down on completion.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


# ── Helper ───────────────────────────────────────────────────────────────────

def _insert_sample_data(conn, schema: str, n_empresas: int = 3) -> None:
    """Insert n_empresas rows into rf_empresas and rf_estabelecimentos."""
    for i in range(1, n_empresas + 1):
        cnpj = str(i).zfill(8)
        conn.execute(
            f"INSERT INTO {schema}.rf_empresas "
            "(cnpj_basico, razao_social, run_key) VALUES (%s, %s, %s)",
            (cnpj, f"EMPRESA {i}", "2026-01"),
        )
        conn.execute(
            f"INSERT INTO {schema}.rf_estabelecimentos "
            "(cnpj_basico, cnpj_ordem, cnpj_dv, run_key) VALUES (%s, %s, %s, %s)",
            (cnpj, "0001", "00", "2026-01"),
        )


def _ddl_patch_load(monkeypatch, schema: str) -> None:
    """
    Patch the DDL generator functions in load_step so the _new tables are created
    in the isolated test schema rather than the hardcoded 'cnpj' schema.
    """
    import steps.load_step as ls

    # DDLs de teste SEM primary key inline — a PK é adicionada pelo load após o
    # dedup (mesmo contrato das DDLs reais).
    monkeypatch.setattr(
        ls, "_empresas_new_ddl",
        lambda rk: f"""
CREATE TABLE {schema}.rf_empresas_new (
    cnpj_basico                  CHAR(8)     NOT NULL,
    razao_social                 TEXT,
    natureza_juridica            CHAR(4),
    qualificacao_responsavel     CHAR(2),
    capital_social               TEXT,
    porte                        CHAR(2),
    ente_federativo_responsavel  TEXT,
    run_key                      CHAR(7)     DEFAULT '{rk}',
    created_at                   TIMESTAMPTZ DEFAULT NOW(),
    updated_at                   TIMESTAMPTZ DEFAULT NOW()
)""",
    )

    monkeypatch.setattr(
        ls, "_estab_new_ddl",
        lambda rk: f"""
CREATE TABLE {schema}.rf_estabelecimentos_new (
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
    run_key                      CHAR(7)     DEFAULT '{rk}',
    created_at                   TIMESTAMPTZ DEFAULT NOW(),
    updated_at                   TIMESTAMPTZ DEFAULT NOW()
)""",
    )

    monkeypatch.setattr(
        ls, "_socios_new_ddl",
        lambda rk: f"""
CREATE TABLE {schema}.rf_socios_new (
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
    run_key                      CHAR(7)     DEFAULT '{rk}',
    created_at                   TIMESTAMPTZ DEFAULT NOW(),
    updated_at                   TIMESTAMPTZ DEFAULT NOW()
)""",
    )

    monkeypatch.setattr(
        ls, "_simples_new_ddl",
        lambda rk: f"""
CREATE TABLE {schema}.rf_simples_new (
    cnpj_basico           CHAR(8)     NOT NULL,
    opcao_pelo_simples    CHAR(1),
    data_opcao_simples    CHAR(8),
    data_exclusao_simples CHAR(8),
    opcao_pelo_mei        CHAR(1),
    data_opcao_mei        CHAR(8),
    data_exclusao_mei     CHAR(8),
    run_key               CHAR(7)     DEFAULT '{rk}',
    updated_at            TIMESTAMPTZ DEFAULT NOW()
)""",
    )


def _table_exists(conn, schema: str, table: str) -> bool:
    """Return True if table exists in schema."""
    row = conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s",
        (schema, table),
    ).fetchone()
    return row is not None


def _count_rows(conn, qualified_table: str) -> int:
    """Return row count for a fully-qualified table."""
    row = conn.execute(f"SELECT COUNT(*) FROM {qualified_table}").fetchone()
    return row[0] if row else 0


# ── Test 1 ───────────────────────────────────────────────────────────────────

def test_build_new_table_does_not_touch_live(test_db, sample_csvs, monkeypatch):
    """
    _build_new_table must create rf_empresas_new without modifying rf_empresas.

    After building the _new table from the CSV:
    - live rf_empresas retains its original 3 rows (run_key "2026-01")
    - rf_empresas_new exists and contains rows from the CSV
    """
    from steps.load_step import _build_new_table, _EMPRESAS_COLS

    schema = test_db["schema"]
    conn   = test_db["conn"]

    _ddl_patch_load(monkeypatch, schema)
    _insert_sample_data(conn, schema, n_empresas=3)

    import steps.load_step as ls
    create_ddl = ls._empresas_new_ddl("2026-02")

    _build_new_table(
        conn,
        new_table=f"{schema}.rf_empresas_new",
        create_ddl=create_ddl,
        csv_path=sample_csvs["empresas"],
        columns=_EMPRESAS_COLS,
        index_sqls=[
            f"CREATE INDEX idx_e_new_test ON {schema}.rf_empresas_new (cnpj_basico)"
        ],
    )

    # Live table must be untouched
    live_count = _count_rows(conn, f"{schema}.rf_empresas")
    assert live_count == 3, f"Live rf_empresas was modified; expected 3 rows, got {live_count}"

    live_run_keys = set(
        r[0] for r in conn.execute(
            f"SELECT DISTINCT run_key FROM {schema}.rf_empresas"
        ).fetchall() if r[0] is not None
    )
    assert live_run_keys == {"2026-01"}, (
        f"Live rf_empresas run_key changed. Found: {live_run_keys}"
    )

    # _new table must exist and have rows
    assert _table_exists(conn, schema, "rf_empresas_new"), "rf_empresas_new was not created"
    new_count = _count_rows(conn, f"{schema}.rf_empresas_new")
    assert new_count > 0, "rf_empresas_new is empty after build"


# ── Test 1b — regressão do bug de cnpj_basico duplicado na fonte RF ──────────

def test_build_new_table_dedups_duplicate_source_key(test_db, tmp_path, monkeypatch):
    """
    Quando o CSV da RF traz o mesmo cnpj_basico duas vezes (registro real + stub
    vazio), _build_new_table com pk_cols deve:
      - manter UMA linha para a chave (a mais completa — o registro real)
      - adicionar a PRIMARY KEY sem UniqueViolation
    Reproduz o incidente de 2026-06 (cnpj_basico 08314885 duplicado).
    """
    from steps.load_step import _build_new_table, _EMPRESAS_COLS

    schema = test_db["schema"]
    conn   = test_db["conn"]

    _ddl_patch_load(monkeypatch, schema)

    # Registro real primeiro, stub vazio depois — e também o caso inverso (stub
    # primeiro) para garantir que a ordem física não decide o vencedor.
    csv_path = tmp_path / "empresas_dup.csv"
    csv_path.write_text(
        "cnpj_basico,razao_social,natureza_juridica,qualificacao_responsavel,"
        "capital_social,porte,ente_federativo_responsavel\n"
        "08314885,FLAVIO PAVAO DE SOUZA,4120,59,\"0,00\",05,\n"
        "08314885,,0000,00,\"0,00\",,\n"
        "00000002,,,,,,\n"                                  # stub primeiro
        "00000002,EMPRESA COMPLETA LTDA,2062,49,\"1000,00\",03,\n",
        encoding="utf-8",
    )

    import steps.load_step as ls
    create_ddl = ls._empresas_new_ddl("2026-06")

    _build_new_table(
        conn,
        new_table=f"{schema}.rf_empresas_new",
        create_ddl=create_ddl,
        csv_path=csv_path,
        columns=_EMPRESAS_COLS,
        index_sqls=[],
        pk_cols=["cnpj_basico"],
    )

    # Uma linha por chave
    assert _count_rows(conn, f"{schema}.rf_empresas_new") == 2

    # A linha mantida é a mais completa (razao_social preenchida) em ambos os casos
    rows = dict(conn.execute(
        f"SELECT cnpj_basico, razao_social FROM {schema}.rf_empresas_new"
    ).fetchall())
    assert rows["08314885"].strip() == "FLAVIO PAVAO DE SOUZA"
    assert rows["00000002"].strip() == "EMPRESA COMPLETA LTDA"

    # A PRIMARY KEY foi criada (chave única garantida daqui pra frente)
    pk = conn.execute(
        "SELECT 1 FROM information_schema.table_constraints "
        "WHERE table_schema=%s AND table_name='rf_empresas_new' "
        "AND constraint_type='PRIMARY KEY'",
        (schema,),
    ).fetchone()
    assert pk is not None, "PRIMARY KEY não foi adicionada após o dedup"


# ── Test 2 ───────────────────────────────────────────────────────────────────

def test_swap_makes_new_data_live(test_db, sample_csvs, monkeypatch):
    """
    After _swap_table:
    - rf_empresas contains the new CSV data (run_key "2026-02" via DEFAULT)
    - rf_empresas_old exists and contains the original data (run_key "2026-01")
    """
    from steps.load_step import _build_new_table, _swap_table, _EMPRESAS_COLS

    schema = test_db["schema"]
    conn   = test_db["conn"]

    _ddl_patch_load(monkeypatch, schema)
    _insert_sample_data(conn, schema, n_empresas=3)

    import steps.load_step as ls
    create_ddl = ls._empresas_new_ddl("2026-02")

    _build_new_table(
        conn,
        new_table=f"{schema}.rf_empresas_new",
        create_ddl=create_ddl,
        csv_path=sample_csvs["empresas"],
        columns=_EMPRESAS_COLS,
        index_sqls=[],
    )
    _swap_table(conn, f"{schema}.rf_empresas")

    # Live table must now have new-run data
    new_run_keys = set(
        r[0] for r in conn.execute(
            f"SELECT DISTINCT run_key FROM {schema}.rf_empresas"
        ).fetchall() if r[0] is not None
    )
    assert "2026-02" in new_run_keys, (
        f"rf_empresas should have run_key '2026-02' after swap. Found: {new_run_keys}"
    )
    assert "2026-01" not in new_run_keys, (
        f"Old run_key '2026-01' still in live table after swap: {new_run_keys}"
    )

    # _old table must exist and hold the original data
    assert _table_exists(conn, schema, "rf_empresas_old"), "rf_empresas_old not created by swap"
    old_run_keys = set(
        r[0] for r in conn.execute(
            f"SELECT DISTINCT run_key FROM {schema}.rf_empresas_old"
        ).fetchall() if r[0] is not None
    )
    assert "2026-01" in old_run_keys, (
        f"rf_empresas_old should have original run_key '2026-01'. Found: {old_run_keys}"
    )


# ── Test 3 ───────────────────────────────────────────────────────────────────

def test_swap_is_atomic_on_failure(test_db, sample_csvs, monkeypatch):
    """
    If a failure occurs BEFORE _swap_table is called, the live table must be
    unchanged and rf_empresas_old must NOT exist.

    The _new table may or may not exist (partial build), but the live data is safe.
    """
    from steps.load_step import _EMPRESAS_COLS
    import psycopg

    schema = test_db["schema"]
    conn   = test_db["conn"]

    _ddl_patch_load(monkeypatch, schema)
    _insert_sample_data(conn, schema, n_empresas=3)

    import steps.load_step as ls
    create_ddl = ls._empresas_new_ddl("2026-02")

    # Simulate failure during index creation by raising before _swap_table
    raised = False
    try:
        # DROP + CREATE + COPY succeed
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {schema}.rf_empresas_new")
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(create_ddl)
        conn.commit()
        ls._copy_csv(conn, f"{schema}.rf_empresas_new", sample_csvs["empresas"], _EMPRESAS_COLS)
        conn.commit()
        # Simulate failure before index/swap
        raise RuntimeError("Simulated index build failure")
    except RuntimeError:
        raised = True
        # Rollback any open transaction
        try:
            conn.rollback()
        except Exception:
            pass

    assert raised, "Exception was not raised (test setup error)"

    # Live table must still have original data
    live_count = _count_rows(conn, f"{schema}.rf_empresas")
    assert live_count == 3, (
        f"Live rf_empresas was corrupted after failure; expected 3 rows, got {live_count}"
    )

    live_run_keys = set(
        r[0] for r in conn.execute(
            f"SELECT DISTINCT run_key FROM {schema}.rf_empresas"
        ).fetchall() if r[0] is not None
    )
    assert "2026-01" in live_run_keys, (
        f"Original run_key missing from live table: {live_run_keys}"
    )

    # _old must NOT exist (swap never happened)
    assert not _table_exists(conn, schema, "rf_empresas_old"), (
        "rf_empresas_old exists even though swap never ran — old data incorrectly marked as old"
    )


# ── Test 4 ───────────────────────────────────────────────────────────────────

def test_drop_previous_old_tables_cascade(test_db, monkeypatch):
    """
    _drop_previous_old_tables must drop rf_empresas_old (with CASCADE) when it exists.
    """
    from steps.load_step import _drop_previous_old_tables

    schema = test_db["schema"]
    conn   = test_db["conn"]

    # Manually create an _old table to simulate a leftover from a previous run
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {schema}.rf_empresas_old "
        f"(LIKE {schema}.rf_empresas INCLUDING ALL)"
    )

    assert _table_exists(conn, schema, "rf_empresas_old"), (
        "Precondition failed: rf_empresas_old was not created"
    )

    _drop_previous_old_tables(conn)

    assert not _table_exists(conn, schema, "rf_empresas_old"), (
        "rf_empresas_old still exists after _drop_previous_old_tables"
    )


# ── Test 5 ───────────────────────────────────────────────────────────────────

def test_full_load_run_success(test_db, sample_csvs, transform_manifest, monkeypatch):
    """
    load_step.run() with a real PostgreSQL must:
    - return StepResult.success
    - write load_manifest.json
    - populate all 4 main tables
    - leave rf_empresas_old etc. in place (swap happened; _old kept for index_step)
    """
    import steps.load_step as ls
    from steps.base import StepStatus

    schema  = test_db["schema"]
    serving = test_db["serving"]

    _ddl_patch_load(monkeypatch, schema)

    # Also patch LOOKUP_TABLE_MAP to point to the test schema
    monkeypatch.setattr(ls, "LOOKUP_TABLE_MAP", {
        "cnaes":    f"{schema}.rf_cnaes",
        "municipios": f"{schema}.rf_municipios",
    })

    checkpoint_dir, _ = transform_manifest

    result = ls.run("job-5", "2026-02", checkpoint_dir)

    assert result.status == StepStatus.SUCCESS, (
        f"Expected SUCCESS, got {result.status}. Error: {result.error}"
    )

    load_manifest_path = checkpoint_dir / "load_manifest.json"
    assert load_manifest_path.exists(), "load_manifest.json was not written"

    data = json.loads(load_manifest_path.read_text(encoding="utf-8"))
    assert isinstance(data.get("tables"), list)

    # All 4 main tables must have rows
    for tbl in ("rf_empresas", "rf_estabelecimentos", "rf_socios", "rf_simples"):
        count = _count_rows(test_db["conn"], f"{schema}.{tbl}")
        assert count > 0, f"{tbl} is empty after successful run"

    # _old tables must exist (swap happened, they are kept for index_step)
    for tbl_old in ("rf_empresas_old", "rf_estabelecimentos_old",
                    "rf_socios_old", "rf_simples_old"):
        assert _table_exists(test_db["conn"], schema, tbl_old), (
            f"{tbl_old} should exist after swap (kept for index_step)"
        )


# ── Test 6 ───────────────────────────────────────────────────────────────────

def test_monthly_update_cycle(test_db, tmp_path, monkeypatch):
    """
    Full two-month update cycle:

    Run 1 (simulated): insert rows with run_key "2026-01" directly.
    Run 2 (load_step): swap in new CSV data with run_key "2026-02".
        - rf_empresas must have run_key "2026-02"
        - rf_empresas_old must have run_key "2026-01"
    index_step: refresh MV, then drop _old tables.
        - MV must be populated
        - rf_empresas_old must no longer exist
    """
    import steps.load_step as ls
    import steps.index_step as idx
    from steps.base import StepStatus

    schema  = test_db["schema"]
    serving = test_db["serving"]
    conn    = test_db["conn"]

    _ddl_patch_load(monkeypatch, schema)

    monkeypatch.setattr(ls, "LOOKUP_TABLE_MAP", {
        "cnaes":    f"{schema}.rf_cnaes",
        "municipios": f"{schema}.rf_municipios",
    })

    # ── Run 1: seed data directly (simulates a first load already done) ──────
    _insert_sample_data(conn, schema, n_empresas=3)

    # ── Build transform manifest for Run 2 ───────────────────────────────────
    out_dir = tmp_path / "transformed_run2"
    out_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_dir = tmp_path / "checkpoints_run2"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    csv_files = {
        "empresas.csv": (
            "cnpj_basico,razao_social,natureza_juridica,qualificacao_responsavel,"
            "capital_social,porte,ente_federativo_responsavel\n"
            "00000001,EMPRESA TESTE 1 v2,2062,49,1000.00,05,\n"
            "00000002,EMPRESA TESTE 2 v2,2062,49,2000.00,03,\n"
            "00000003,EMPRESA TESTE 3 v2,2062,49,3000.00,01,\n"
        ),
        "estabelecimentos.csv": (
            "cnpj_basico,cnpj_ordem,cnpj_dv,identificador_matriz_filial,nome_fantasia,"
            "situacao_cadastral,data_situacao_cadastral,motivo_situacao_cadastral,"
            "nm_cidade_exterior,pais,data_inicio_atividade,cnae_fiscal,"
            "cnae_fiscal_secundaria,tipo_logradouro,logradouro,numero,complemento,"
            "bairro,cep,uf,municipio,ddd1,telefone1,ddd2,telefone2,ddd_fax,fax,"
            "correio_eletronico,situacao_especial,data_situacao_especial\n"
            "00000001,0001,00,1,,02,20200101,,,,20200101,6201500,,RUA,TESTE,1,,CENTRO,01310100,SP,7107,11,11111111,,,,,,,,\n"
            "00000002,0001,00,1,,02,20200201,,,,20200201,6201500,,RUA,TESTE,2,,CENTRO,01310100,SP,7107,11,22222222,,,,,,,,\n"
            "00000003,0001,00,1,,02,20200301,,,,20200301,6201500,,RUA,TESTE,3,,CENTRO,01310100,SP,7107,11,33333333,,,,,,,,\n"
        ),
        "socios.csv": (
            "cnpj_basico,identificador_socio,nome_socio,cnpj_cpf_socio,"
            "qualificacao_socio,data_entrada_sociedade,pais,representante_legal,"
            "nome_representante,qualificacao_representante,faixa_etaria\n"
            "00000001,2,SOCIO A,12345678901,49,20200101,,,,,4\n"
        ),
        "simples.csv": (
            "cnpj_basico,opcao_pelo_simples,data_opcao_simples,data_exclusao_simples,"
            "opcao_pelo_mei,data_opcao_mei,data_exclusao_mei\n"
            "00000001,S,20200101,,N,,\n"
        ),
        "cnaes.csv": "codigo,descricao\n6201500,TESTE CNAE\n",
        "municipios.csv": "codigo,descricao\n7107,SAO PAULO\n",
    }

    for fname, content in csv_files.items():
        (out_dir / fname).write_text(content, encoding="utf-8")

    manifest = {
        "run_key": "2026-02",
        "out_dir": str(out_dir),
        "tables": [
            {"keyword": "Empresas",         "rows": 3, "out": str(out_dir / "empresas.csv")},
            {"keyword": "Estabelecimentos",  "rows": 3, "out": str(out_dir / "estabelecimentos.csv")},
            {"keyword": "Socios",            "rows": 1, "out": str(out_dir / "socios.csv")},
            {"keyword": "Simples",           "rows": 1, "out": str(out_dir / "simples.csv")},
            {"keyword": "cnaes",             "rows": 1, "out": str(out_dir / "cnaes.csv")},
            {"keyword": "municipios",        "rows": 1, "out": str(out_dir / "municipios.csv")},
        ],
    }
    (checkpoint_dir / "transform_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    # ── Run 2: load_step ──────────────────────────────────────────────────────
    result = ls.run("job-6", "2026-02", checkpoint_dir)
    assert result.status == StepStatus.SUCCESS, (
        f"load_step run 2 failed: {result.error}"
    )

    # Verify new data is live
    live_rks = set(
        r[0] for r in conn.execute(
            f"SELECT DISTINCT run_key FROM {schema}.rf_empresas"
        ).fetchall() if r[0] is not None
    )
    assert "2026-02" in live_rks, (
        f"rf_empresas should have run_key '2026-02' after run 2. Found: {live_rks}"
    )

    # Verify old data in _old table
    old_rks = set(
        r[0] for r in conn.execute(
            f"SELECT DISTINCT run_key FROM {schema}.rf_empresas_old"
        ).fetchall() if r[0] is not None
    )
    assert "2026-01" in old_rks, (
        f"rf_empresas_old should have run_key '2026-01'. Found: {old_rks}"
    )

    # ── index_step ────────────────────────────────────────────────────────────
    idx_result = idx.run("job-6", "2026-02", checkpoint_dir)
    assert idx_result.status == StepStatus.SUCCESS, (
        f"index_step failed: {idx_result.error}"
    )

    # MV must be populated
    mv_count = _count_rows(conn, f"{serving}.mv_cnpj_full")
    assert mv_count > 0, "mv_cnpj_full is empty after index_step"

    # _old tables must be cleaned up by index_step
    for tbl_old in ("rf_empresas_old", "rf_estabelecimentos_old",
                    "rf_socios_old", "rf_simples_old"):
        assert not _table_exists(conn, schema, tbl_old), (
            f"{tbl_old} still exists after index_step — should have been cleaned up"
        )


# ── Test 7 ───────────────────────────────────────────────────────────────────

def test_index_step_recreates_missing_mv(test_db, tmp_path, monkeypatch):
    """
    When mv_cnpj_full is manually dropped, index_step.run() must recreate it and
    populate it with the current data.
    """
    import steps.index_step as idx
    from steps.base import StepStatus

    schema  = test_db["schema"]
    serving = test_db["serving"]
    conn    = test_db["conn"]

    # Seed data so MV has something to read after REFRESH
    _insert_sample_data(conn, schema, n_empresas=2)

    # Drop the MV to simulate a missing state
    conn.execute(f"DROP MATERIALIZED VIEW IF EXISTS {serving}.mv_cnpj_full CASCADE")

    checkpoint_dir = tmp_path / "checkpoints_t7"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    result = idx.run("job-7", "2026-01", checkpoint_dir)
    assert result.status == StepStatus.SUCCESS, (
        f"index_step failed when MV was missing: {result.error}"
    )

    # MV must exist and be populated
    mv_exists = conn.execute(
        "SELECT 1 FROM pg_matviews WHERE schemaname = %s AND matviewname = 'mv_cnpj_full'",
        (serving,),
    ).fetchone()
    assert mv_exists, "mv_cnpj_full was not recreated by index_step"

    mv_count = _count_rows(conn, f"{serving}.mv_cnpj_full")
    assert mv_count > 0, "mv_cnpj_full is empty after recreation"


# ── Test 8 ───────────────────────────────────────────────────────────────────

def test_index_step_drops_old_tables_after_refresh(test_db, tmp_path, monkeypatch):
    """
    After index_step.run() the four _old tables must no longer exist,
    having been cleaned up once the MV was safely refreshed against the new tables.
    """
    import steps.index_step as idx
    from steps.base import StepStatus

    schema  = test_db["schema"]
    serving = test_db["serving"]
    conn    = test_db["conn"]

    # Seed live data
    _insert_sample_data(conn, schema, n_empresas=3)

    # Manually create _old tables (simulating a leftover swap from load_step)
    for tbl in ("rf_empresas", "rf_estabelecimentos"):
        conn.execute(
            f"CREATE TABLE {schema}.{tbl}_old "
            f"(LIKE {schema}.{tbl} INCLUDING ALL)"
        )

    conn.execute(
        f"CREATE TABLE {schema}.rf_socios_old "
        f"(LIKE {schema}.rf_socios INCLUDING ALL)"
    )
    conn.execute(
        f"CREATE TABLE {schema}.rf_simples_old "
        f"(LIKE {schema}.rf_simples INCLUDING ALL)"
    )

    # Refresh MV so it is populated (non-concurrently, since unique index exists)
    conn.execute(f"REFRESH MATERIALIZED VIEW {serving}.mv_cnpj_full")

    checkpoint_dir = tmp_path / "checkpoints_t8"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    # index_step should do a CONCURRENT refresh (MV is populated) and then drop _old
    result = idx.run("job-8", "2026-01", checkpoint_dir)
    assert result.status == StepStatus.SUCCESS, (
        f"index_step failed: {result.error}"
    )

    # All _old tables must be gone
    for tbl_old in ("rf_empresas_old", "rf_estabelecimentos_old",
                    "rf_socios_old", "rf_simples_old"):
        assert not _table_exists(conn, schema, tbl_old), (
            f"{tbl_old} still exists after index_step cleanup"
        )
