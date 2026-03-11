"""
conftest.py — shared fixtures for load/index step tests.

Provides:
- sample_csvs: minimal UTF-8 CSV files matching the actual column schemas
- transform_manifest: writes transform_manifest.json to a temp checkpoint dir
- pg_conn: raw psycopg connection to a test PostgreSQL instance
- test_db: isolated test schemas with all tables and a simplified MV
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

# ── CSV content constants ────────────────────────────────────────────────────

_EMPRESAS_CSV = """\
cnpj_basico,razao_social,natureza_juridica,qualificacao_responsavel,capital_social,porte,ente_federativo_responsavel
00000001,EMPRESA TESTE 1,2062,49,1000.00,05,
00000002,EMPRESA TESTE 2,2062,49,2000.00,03,
00000003,EMPRESA TESTE 3,2062,49,3000.00,01,
"""

_ESTAB_CSV = """\
cnpj_basico,cnpj_ordem,cnpj_dv,identificador_matriz_filial,nome_fantasia,situacao_cadastral,data_situacao_cadastral,motivo_situacao_cadastral,nm_cidade_exterior,pais,data_inicio_atividade,cnae_fiscal,cnae_fiscal_secundaria,tipo_logradouro,logradouro,numero,complemento,bairro,cep,uf,municipio,ddd1,telefone1,ddd2,telefone2,ddd_fax,fax,correio_eletronico,situacao_especial,data_situacao_especial
00000001,0001,50,1,,02,20200101,,,,20200101,6201500,,RUA,TESTE,123,,CENTRO,01310100,SP,7107,11,11111111,,,,,,,,
00000002,0001,50,1,,02,20200201,,,,20200201,6201500,,RUA,TESTE,456,,CENTRO,01310100,SP,7107,11,22222222,,,,,,,,
00000003,0001,50,1,,02,20200301,,,,20200301,6201500,,RUA,TESTE,789,,CENTRO,01310100,SP,7107,11,33333333,,,,,,,,
"""

_SOCIOS_CSV = """\
cnpj_basico,identificador_socio,nome_socio,cnpj_cpf_socio,qualificacao_socio,data_entrada_sociedade,pais,representante_legal,nome_representante,qualificacao_representante,faixa_etaria
00000001,2,JOAO DA SILVA,12345678901,49,20200101,,,,,4
00000002,2,MARIA SANTOS,98765432100,49,20200201,,,,,3
00000003,2,PEDRO OLIVEIRA,11122233344,49,20200301,,,,,5
"""

_SIMPLES_CSV = """\
cnpj_basico,opcao_pelo_simples,data_opcao_simples,data_exclusao_simples,opcao_pelo_mei,data_opcao_mei,data_exclusao_mei
00000001,S,20200101,,N,,
00000002,N,,,S,20200201,
00000003,S,20200101,,N,,
"""

_CNAES_CSV = """\
codigo,descricao
6201500,DESENVOLVIMENTO DE PROGRAMAS DE COMPUTADOR SOB ENCOMENDA
6202300,DESENVOLVIMENTO E LICENCIAMENTO DE PROGRAMAS CUSTOMIZAVEIS
"""

_MUNICIPIOS_CSV = """\
codigo,descricao
7107,SAO PAULO
6291,RIO DE JANEIRO
"""


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_csvs(tmp_path: Path) -> dict:
    """
    Creates minimal UTF-8 CSV files for each table under tmp_path/transformed/.

    Returns a dict with keys: dir, empresas, estabelecimentos, socios, simples,
    cnaes, municipios — all Path objects.
    """
    out = tmp_path / "transformed"
    out.mkdir(parents=True, exist_ok=True)

    files = {
        "empresas":        ("empresas.csv",        _EMPRESAS_CSV),
        "estabelecimentos":("estabelecimentos.csv", _ESTAB_CSV),
        "socios":          ("socios.csv",           _SOCIOS_CSV),
        "simples":         ("simples.csv",          _SIMPLES_CSV),
        "cnaes":           ("cnaes.csv",            _CNAES_CSV),
        "municipios":      ("municipios.csv",        _MUNICIPIOS_CSV),
    }

    result: dict = {"dir": out}
    for key, (fname, content) in files.items():
        p = out / fname
        p.write_text(content, encoding="utf-8")
        result[key] = p

    return result


@pytest.fixture
def transform_manifest(tmp_path: Path, sample_csvs: dict):
    """
    Writes a transform_manifest.json to tmp_path/checkpoints/.

    Returns (checkpoint_dir: Path, manifest_path: Path).
    """
    checkpoint_dir = tmp_path / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    out_dir = sample_csvs["dir"]
    manifest = {
        "run_key": "2026-02",
        "out_dir": str(out_dir),
        "tables": [
            {"keyword": "Empresas",        "rows": 3, "out": str(sample_csvs["empresas"])},
            {"keyword": "Estabelecimentos","rows": 3, "out": str(sample_csvs["estabelecimentos"])},
            {"keyword": "Socios",          "rows": 3, "out": str(sample_csvs["socios"])},
            {"keyword": "Simples",         "rows": 3, "out": str(sample_csvs["simples"])},
            {"keyword": "cnaes",           "rows": 2, "out": str(sample_csvs["cnaes"])},
            {"keyword": "municipios",      "rows": 2, "out": str(sample_csvs["municipios"])},
        ],
    }

    manifest_path = checkpoint_dir / "transform_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    return checkpoint_dir, manifest_path


@pytest.fixture
def pg_conn():
    """
    Yields a psycopg.Connection with autocommit=True to the test database.

    Reads TEST_POSTGRES_URL (falls back to POSTGRES_URL). Skips if neither is set.
    """
    import psycopg

    url = os.environ.get("TEST_POSTGRES_URL") or os.environ.get("POSTGRES_URL", "")
    if not url:
        pytest.skip("TEST_POSTGRES_URL not set — skipping integration test")

    with psycopg.connect(url, autocommit=True) as conn:
        yield conn


@pytest.fixture
def test_db(pg_conn, monkeypatch):
    """
    Creates isolated test schemas with a unique timestamp suffix, populates all
    necessary tables and a simplified materialized view, then patches settings and
    module-level variables in load_step / index_step.

    Yields {"schema": str, "serving": str, "conn": pg_conn}.
    Drops schemas on teardown.
    """
    suffix = str(int(time.time() * 1000))[-8:]
    schema  = f"test_cnpj_{suffix}"
    serving = f"test_serving_{suffix}"

    conn = pg_conn

    # ── Create schemas ────────────────────────────────────────────────────────
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {serving}")

    # ── Main tables ───────────────────────────────────────────────────────────
    conn.execute(f"""
        CREATE TABLE {schema}.rf_empresas (
            cnpj_basico                 CHAR(8)     NOT NULL PRIMARY KEY,
            razao_social                TEXT,
            natureza_juridica           CHAR(4),
            qualificacao_responsavel    CHAR(2),
            capital_social              TEXT,
            porte                       CHAR(2),
            ente_federativo_responsavel TEXT,
            run_key                     CHAR(7),
            created_at                  TIMESTAMPTZ DEFAULT NOW(),
            updated_at                  TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    conn.execute(f"""
        CREATE TABLE {schema}.rf_estabelecimentos (
            cnpj_basico                 CHAR(8)     NOT NULL,
            cnpj_ordem                  CHAR(4)     NOT NULL,
            cnpj_dv                     CHAR(2)     NOT NULL,
            identificador_matriz_filial CHAR(1),
            nome_fantasia               TEXT,
            situacao_cadastral          CHAR(2),
            data_situacao_cadastral     CHAR(8),
            motivo_situacao_cadastral   CHAR(2),
            nm_cidade_exterior          TEXT,
            pais                        CHAR(3),
            data_inicio_atividade       CHAR(8),
            cnae_fiscal                 CHAR(7),
            cnae_fiscal_secundaria      TEXT,
            tipo_logradouro             TEXT,
            logradouro                  TEXT,
            numero                      TEXT,
            complemento                 TEXT,
            bairro                      TEXT,
            cep                         CHAR(8),
            uf                          CHAR(2),
            municipio                   CHAR(7),
            ddd1                        TEXT,
            telefone1                   TEXT,
            ddd2                        TEXT,
            telefone2                   TEXT,
            ddd_fax                     TEXT,
            fax                         TEXT,
            correio_eletronico          TEXT,
            situacao_especial           TEXT,
            data_situacao_especial      CHAR(8),
            run_key                     CHAR(7),
            created_at                  TIMESTAMPTZ DEFAULT NOW(),
            updated_at                  TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (cnpj_basico, cnpj_ordem, cnpj_dv)
        )
    """)

    conn.execute(f"""
        CREATE TABLE {schema}.rf_socios (
            id                          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            cnpj_basico                 CHAR(8),
            identificador_socio         CHAR(1),
            nome_socio                  TEXT,
            cnpj_cpf_socio              TEXT,
            qualificacao_socio          CHAR(2),
            data_entrada_sociedade      CHAR(8),
            pais                        CHAR(3),
            representante_legal         TEXT,
            nome_representante          TEXT,
            qualificacao_representante  CHAR(2),
            faixa_etaria                CHAR(1),
            run_key                     CHAR(7),
            created_at                  TIMESTAMPTZ DEFAULT NOW(),
            updated_at                  TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    conn.execute(f"""
        CREATE TABLE {schema}.rf_simples (
            cnpj_basico           CHAR(8)     NOT NULL PRIMARY KEY,
            opcao_pelo_simples    CHAR(1),
            data_opcao_simples    CHAR(8),
            data_exclusao_simples CHAR(8),
            opcao_pelo_mei        CHAR(1),
            data_opcao_mei        CHAR(8),
            data_exclusao_mei     CHAR(8),
            run_key               CHAR(7),
            updated_at            TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    # ── Lookup tables ─────────────────────────────────────────────────────────
    conn.execute(f"CREATE TABLE {schema}.rf_cnaes       (codigo CHAR(7) PRIMARY KEY, descricao TEXT)")
    conn.execute(f"CREATE TABLE {schema}.rf_municipios  (codigo CHAR(7) PRIMARY KEY, descricao TEXT)")
    conn.execute(f"CREATE TABLE {schema}.rf_naturezas   (codigo CHAR(4) PRIMARY KEY, descricao TEXT)")
    conn.execute(f"CREATE TABLE {schema}.rf_qualificacoes (codigo CHAR(2) PRIMARY KEY, descricao TEXT)")
    conn.execute(f"CREATE TABLE {schema}.rf_motivos     (codigo CHAR(2) PRIMARY KEY, descricao TEXT)")
    conn.execute(f"CREATE TABLE {schema}.rf_paises      (codigo CHAR(3) PRIMARY KEY, descricao TEXT)")
    conn.execute(f"CREATE TABLE {schema}.rf_portes      (codigo CHAR(2) PRIMARY KEY, descricao TEXT)")

    # ── Simplified materialized view (no pg_trgm needed) ─────────────────────
    conn.execute(f"""
        CREATE MATERIALIZED VIEW {serving}.mv_cnpj_full AS
        SELECT
            e.cnpj_basico || est.cnpj_ordem || est.cnpj_dv AS cnpj_completo,
            e.cnpj_basico,
            est.cnpj_ordem,
            est.cnpj_dv,
            e.razao_social,
            e.run_key,
            e.updated_at
        FROM {schema}.rf_estabelecimentos est
        JOIN {schema}.rf_empresas e ON e.cnpj_basico = est.cnpj_basico
        WITH NO DATA
    """)
    conn.execute(
        f"CREATE UNIQUE INDEX idx_test_mv_cnpj_{suffix} "
        f"ON {serving}.mv_cnpj_full (cnpj_completo)"
    )

    # ── Patch module-level variables ──────────────────────────────────────────
    import steps.load_step as load_step_mod
    import steps.index_step as index_step_mod

    main_tables = [
        f"{schema}.rf_empresas",
        f"{schema}.rf_estabelecimentos",
        f"{schema}.rf_socios",
        f"{schema}.rf_simples",
    ]

    monkeypatch.setattr(load_step_mod, "_D", schema)
    monkeypatch.setattr(load_step_mod, "_MAIN_TABLES", main_tables)

    monkeypatch.setattr(index_step_mod, "_D", schema)
    monkeypatch.setattr(index_step_mod, "_V", serving)
    monkeypatch.setattr(index_step_mod, "_MAIN_TABLES", main_tables)

    # Simplified MV DDL (no trigram indexes)
    test_mv_ddl = f"""
CREATE MATERIALIZED VIEW {serving}.mv_cnpj_full AS
SELECT e.cnpj_basico || est.cnpj_ordem || est.cnpj_dv AS cnpj_completo,
       e.cnpj_basico, est.cnpj_ordem, est.cnpj_dv,
       e.razao_social, e.run_key, e.updated_at
FROM {schema}.rf_estabelecimentos est
JOIN {schema}.rf_empresas e ON e.cnpj_basico = est.cnpj_basico
WITH NO DATA
"""
    test_mv_indexes = [
        f"CREATE UNIQUE INDEX IF NOT EXISTS idx_test_mv_cnpj_{suffix}_new "
        f"ON {serving}.mv_cnpj_full (cnpj_completo)"
    ]
    monkeypatch.setattr(index_step_mod, "_MV_DDL", test_mv_ddl)
    monkeypatch.setattr(index_step_mod, "_MV_INDEXES", test_mv_indexes)

    # Patch config.settings
    import config
    monkeypatch.setattr(config.settings, "pg_schema", schema)
    monkeypatch.setattr(config.settings, "pg_serving_schema", serving)

    url = os.environ.get("TEST_POSTGRES_URL") or os.environ.get("POSTGRES_URL", "")
    monkeypatch.setattr(config.settings, "postgres_url", url)

    yield {"schema": schema, "serving": serving, "conn": conn}

    # ── Teardown ──────────────────────────────────────────────────────────────
    conn.execute(f"DROP SCHEMA IF EXISTS {serving} CASCADE")
    conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
