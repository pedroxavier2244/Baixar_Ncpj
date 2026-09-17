"""
Teste E2E completo do pipeline.

Requer banco PostgreSQL real. Configure:
    TEST_POSTGRES_URL=postgresql://user:pass@host:5432/cnpj_test

Pula automaticamente se TEST_POSTGRES_URL não estiver definido.

Fluxo testado:
  1. Aplica schema.sql no banco de teste
  2. Roda transform_step (CSV RF → CSV UTF-8 com header)
  3. Roda load_step (COPY → staging → diff_merge → main)
  4. Roda index_step (REFRESH MATERIALIZED VIEW)
  5. Valida dados no banco (contagens, campos, integridade)
  6. Sobe API FastAPI e testa todos os endpoints
"""
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import psycopg
import pytest
from psycopg.rows import dict_row
from starlette.testclient import TestClient

from steps.transform_step import run as transform_run
from steps.load_step import run as load_run
from steps.index_step import run as index_run

_SCHEMA_SQL = Path(__file__).parent.parent / "db" / "schema.sql"

# ── Fixture: banco de teste ───────────────────────────────────────────────────

@pytest.fixture(scope="module")
def test_db_url():
    url = os.environ.get("TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("TEST_POSTGRES_URL não definido — pulando E2E")
    return url


@pytest.fixture(scope="module")
def clean_db(test_db_url):
    """Aplica schema.sql. Ao final, dropa os schemas de teste."""
    schema_sql = _SCHEMA_SQL.read_text(encoding="utf-8")
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        conn.execute(schema_sql)
    yield test_db_url
    # Cleanup — remove tudo que foi criado
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS cnpj CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS cnpj_staging CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS cnpj_serving CASCADE")


# ── Fixture: pipeline completo ────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pipeline_artifacts(clean_db, tmp_path_factory, rf_csv_dir_module):
    """Executa transform → load → index e retorna os artefatos."""
    base = tmp_path_factory.mktemp("pipeline")
    checkpoint_dir = base / "checkpoints"
    checkpoint_dir.mkdir()

    # Escreve extract_manifest (normalmente criado pelo extract_step)
    extract_manifest = {
        "run_key": "2026-01",
        "csv_dir": str(rf_csv_dir_module),
    }
    (checkpoint_dir / "extract_manifest.json").write_text(
        json.dumps(extract_manifest), encoding="utf-8"
    )

    # Step 1: Transform
    with patch("steps.transform_step.settings.data_dir", base):
        t_result = transform_run("job-e2e", "2026-01", checkpoint_dir)
    assert t_result.status == "SUCCESS", f"Transform falhou: {t_result.error}"

    # Step 2: Load
    with patch("steps.load_step.settings.postgres_url", clean_db), \
         patch("steps.load_step.settings.pg_schema", "cnpj"), \
         patch("steps.load_step.settings.pg_staging_schema", "cnpj_staging"), \
         patch("steps.load_step._D", "cnpj"), \
         patch("steps.load_step._S", "cnpj_staging"):
        l_result = load_run("job-e2e", "2026-01", checkpoint_dir)
    assert l_result.status == "SUCCESS", f"Load falhou: {l_result.error}"

    # Step 3: Index
    with patch("steps.index_step.settings.postgres_url", clean_db), \
         patch("steps.index_step.settings.pg_serving_schema", "cnpj_serving"):
        i_result = index_run("job-e2e", "2026-01", checkpoint_dir)
    assert i_result.status == "SUCCESS", f"Index falhou: {i_result.error}"

    return {
        "transform": t_result,
        "load": l_result,
        "index": i_result,
        "db_url": clean_db,
        "checkpoint_dir": checkpoint_dir,
    }


@pytest.fixture(scope="module")
def rf_csv_dir_module(tmp_path_factory):
    """Versão module-scoped do rf_csv_dir."""
    import csv
    from tests.conftest import (
        RF_EMPRESAS, RF_ESTABELECIMENTOS, RF_SOCIOS, RF_SIMPLES,
        RF_CNAES, RF_MUNICIPIOS, RF_NATUREZAS, RF_QUALIFICACOES,
        RF_MOTIVOS, RF_PAISES, _write_rf_csv,
    )
    d = tmp_path_factory.mktemp("rf_csv") / "csv"
    d.mkdir()
    _write_rf_csv(d / "K3241.K03200Y0.D51213.EMPRECSV",  RF_EMPRESAS)
    _write_rf_csv(d / "K3241.K03200Y0.D51213.ESTABELE",  RF_ESTABELECIMENTOS)
    _write_rf_csv(d / "K3241.K03200Y0.D51213.SOCIOCSV",  RF_SOCIOS)
    _write_rf_csv(d / "F.K03200$W.SIMPLES.CSV.D51213",   RF_SIMPLES)
    _write_rf_csv(d / "F.K03200$Z.D51213.CNAECSV",       RF_CNAES)
    _write_rf_csv(d / "F.K03200$Z.D51213.MUNICCSV",      RF_MUNICIPIOS)
    _write_rf_csv(d / "F.K03200$Z.D51213.NATJUCSV",      RF_NATUREZAS)
    _write_rf_csv(d / "F.K03200$Z.D51213.QUALSCSV",      RF_QUALIFICACOES)
    _write_rf_csv(d / "F.K03200$Z.D51213.MOTICSV",       RF_MOTIVOS)
    _write_rf_csv(d / "F.K03200$Z.D51213.PAISCSV",       RF_PAISES)
    return d


# ── Testes do banco de dados ──────────────────────────────────────────────────

class TestDatabaseAfterPipeline:
    def test_schema_cnpj_existe(self, pipeline_artifacts):
        url = pipeline_artifacts["db_url"]
        with psycopg.connect(url) as conn:
            row = conn.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'cnpj'"
            ).fetchone()
        assert row is not None, "Schema 'cnpj' não foi criado"

    def test_schema_staging_existe(self, pipeline_artifacts):
        url = pipeline_artifacts["db_url"]
        with psycopg.connect(url) as conn:
            row = conn.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'cnpj_staging'"
            ).fetchone()
        assert row is not None

    def test_schema_serving_existe(self, pipeline_artifacts):
        url = pipeline_artifacts["db_url"]
        with psycopg.connect(url) as conn:
            row = conn.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'cnpj_serving'"
            ).fetchone()
        assert row is not None

    def test_empresas_tem_3_registros(self, pipeline_artifacts):
        url = pipeline_artifacts["db_url"]
        with psycopg.connect(url) as conn:
            count = conn.execute("SELECT COUNT(*) FROM cnpj.rf_empresas").fetchone()[0]
        assert count == 3

    def test_estabelecimentos_tem_3_registros(self, pipeline_artifacts):
        url = pipeline_artifacts["db_url"]
        with psycopg.connect(url) as conn:
            count = conn.execute("SELECT COUNT(*) FROM cnpj.rf_estabelecimentos").fetchone()[0]
        assert count == 3

    def test_socios_tem_3_registros(self, pipeline_artifacts):
        url = pipeline_artifacts["db_url"]
        with psycopg.connect(url) as conn:
            count = conn.execute("SELECT COUNT(*) FROM cnpj.rf_socios").fetchone()[0]
        assert count == 3

    def test_simples_tem_3_registros(self, pipeline_artifacts):
        url = pipeline_artifacts["db_url"]
        with psycopg.connect(url) as conn:
            count = conn.execute("SELECT COUNT(*) FROM cnpj.rf_simples").fetchone()[0]
        assert count == 3

    def test_cnaes_carregados(self, pipeline_artifacts):
        url = pipeline_artifacts["db_url"]
        with psycopg.connect(url) as conn:
            count = conn.execute("SELECT COUNT(*) FROM cnpj.rf_cnaes").fetchone()[0]
        assert count == 3

    def test_municipios_carregados(self, pipeline_artifacts):
        url = pipeline_artifacts["db_url"]
        with psycopg.connect(url) as conn:
            count = conn.execute("SELECT COUNT(*) FROM cnpj.rf_municipios").fetchone()[0]
        assert count == 2

    def test_empresa_alpha_dados_corretos(self, pipeline_artifacts):
        url = pipeline_artifacts["db_url"]
        with psycopg.connect(url, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT * FROM cnpj.rf_empresas WHERE cnpj_basico = '11111111'"
            ).fetchone()
        assert row is not None
        assert row["razao_social"] == "EMPRESA ALPHA LTDA"
        assert row["natureza_juridica"] == "2062"
        assert row["run_key"] == "2026-01"

    def test_estabelecimento_sp(self, pipeline_artifacts):
        url = pipeline_artifacts["db_url"]
        with psycopg.connect(url, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT * FROM cnpj.rf_estabelecimentos WHERE cnpj_basico = '11111111'"
            ).fetchone()
        assert row is not None
        assert row["uf"] == "SP"
        assert row["nome_fantasia"] == "ALPHA STORE"

    def test_simples_optante(self, pipeline_artifacts):
        url = pipeline_artifacts["db_url"]
        with psycopg.connect(url, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT * FROM cnpj.rf_simples WHERE cnpj_basico = '11111111'"
            ).fetchone()
        assert row is not None
        assert row["opcao_pelo_simples"] == "S"

    def test_mei_gamma(self, pipeline_artifacts):
        url = pipeline_artifacts["db_url"]
        with psycopg.connect(url, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT * FROM cnpj.rf_simples WHERE cnpj_basico = '33333333'"
            ).fetchone()
        assert row is not None
        assert row["opcao_pelo_mei"] == "S"

    def test_mv_cnpj_full_populada(self, pipeline_artifacts):
        url = pipeline_artifacts["db_url"]
        with psycopg.connect(url) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM cnpj_serving.mv_cnpj_full"
            ).fetchone()[0]
        assert count == 3, f"mv_cnpj_full tem {count} linhas, esperava 3"

    def test_mv_join_empresa_e_estabelecimento(self, pipeline_artifacts):
        """MV deve ter cnpj_completo (14 dígitos) com dados de empresa e estabelecimento."""
        url = pipeline_artifacts["db_url"]
        with psycopg.connect(url, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT * FROM cnpj_serving.mv_cnpj_full WHERE cnpj_basico = '11111111'"
            ).fetchone()
        assert row is not None
        assert row["cnpj_completo"] == "11111111000141"
        assert row["razao_social"] == "EMPRESA ALPHA LTDA"
        assert row["uf"] == "SP"
        assert row["municipio_descricao"] == "SAO PAULO"
        assert row["cnae_fiscal_descricao"] is not None

    def test_mv_inclui_simples(self, pipeline_artifacts):
        url = pipeline_artifacts["db_url"]
        with psycopg.connect(url, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT opcao_pelo_simples, opcao_pelo_mei FROM cnpj_serving.mv_cnpj_full "
                "WHERE cnpj_basico = '11111111'"
            ).fetchone()
        assert row["opcao_pelo_simples"] == "S"
        assert row["opcao_pelo_mei"] == "N"

    def test_indice_cnpj_completo_existe(self, pipeline_artifacts):
        url = pipeline_artifacts["db_url"]
        with psycopg.connect(url) as conn:
            row = conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'cnpj_serving' AND indexname = 'idx_mv_cnpj_completo'"
            ).fetchone()
        assert row is not None, "Índice UNIQUE em cnpj_completo não encontrado"

    def test_indice_trigram_razao_social_existe(self, pipeline_artifacts):
        url = pipeline_artifacts["db_url"]
        with psycopg.connect(url) as conn:
            row = conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'cnpj_serving' AND indexname = 'idx_mv_razao_social_trgm'"
            ).fetchone()
        assert row is not None, "Índice GIN trigram em razao_social não encontrado"

    def test_run_key_registrado(self, pipeline_artifacts):
        """Cada empresa deve ter run_key=2026-01 após a carga."""
        url = pipeline_artifacts["db_url"]
        with psycopg.connect(url) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM cnpj.rf_empresas WHERE run_key = '2026-01'"
            ).fetchone()[0]
        assert count == 3

    def test_updated_at_preenchido(self, pipeline_artifacts):
        url = pipeline_artifacts["db_url"]
        with psycopg.connect(url) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM cnpj.rf_empresas WHERE updated_at IS NOT NULL"
            ).fetchone()[0]
        assert count == 3


# ── Testes da API contra banco real ──────────────────────────────────────────

@pytest.fixture(scope="module")
def api_client_e2e(pipeline_artifacts):
    """TestClient com banco real e Redis mockado."""
    from api.main import app
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.setex = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with patch("api.main.aioredis.from_url", return_value=mock_redis), \
         patch("config.settings.postgres_url", pipeline_artifacts["db_url"]):
        with TestClient(app) as c:
            yield c


class TestApiEndToEnd:
    def test_health_ok(self, api_client_e2e):
        r = api_client_e2e.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["mv_row_count"] == 3

    def test_get_cnpj_completo(self, api_client_e2e):
        r = api_client_e2e.get("/cnpj/11111111000141")
        assert r.status_code == 200
        data = r.json()
        assert data["cnpj_completo"] == "11111111000141"
        assert data["razao_social"] == "EMPRESA ALPHA LTDA"
        assert data["uf"] == "SP"
        assert data["municipio_descricao"] == "SAO PAULO"
        assert data["opcao_pelo_simples"] == "S"
        assert data["run_key"] == "2026-01"

    def test_get_cnpj_base(self, api_client_e2e):
        r = api_client_e2e.get("/cnpj/22222222")
        assert r.status_code == 200
        assert r.json()["cnpj_basico"] == "22222222"

    def test_get_cnpj_inexistente_retorna_404(self, api_client_e2e):
        r = api_client_e2e.get("/cnpj/99999999000199")
        assert r.status_code == 404

    def test_get_cnpj_invalido_retorna_400(self, api_client_e2e):
        r = api_client_e2e.get("/cnpj/abc123")
        assert r.status_code == 400

    def test_search_por_uf_sp(self, api_client_e2e):
        r = api_client_e2e.get("/search?uf=SP")
        assert r.status_code == 200
        results = r.json()
        assert len(results) == 2
        assert all(e["uf"] == "SP" for e in results)

    def test_search_por_uf_mg(self, api_client_e2e):
        r = api_client_e2e.get("/search?uf=MG")
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["cnpj_basico"] == "33333333"

    def test_search_por_razao_social(self, api_client_e2e):
        r = api_client_e2e.get("/search?razao_social=ALPHA")
        assert r.status_code == 200
        results = r.json()
        assert len(results) >= 1
        assert any("ALPHA" in e["razao_social"] for e in results)

    def test_search_por_cnae(self, api_client_e2e):
        r = api_client_e2e.get("/search?cnae=6201500")
        assert r.status_code == 200
        assert len(r.json()) == 1

    def test_search_por_situacao(self, api_client_e2e):
        r = api_client_e2e.get("/search?situacao=02")
        assert r.status_code == 200
        assert len(r.json()) == 3

    def test_search_sem_resultado(self, api_client_e2e):
        r = api_client_e2e.get("/search?razao_social=EMPRESA_INEXISTENTE_XYZXYZ")
        assert r.status_code == 200
        assert r.json() == []

    def test_search_limit_respeitado(self, api_client_e2e):
        r = api_client_e2e.get("/search?situacao=02&limit=1")
        assert r.status_code == 200
        assert len(r.json()) == 1

    def test_search_municipio_descricao(self, api_client_e2e):
        r = api_client_e2e.get("/search?municipio=paulo")
        assert r.status_code == 200
        results = r.json()
        assert len(results) >= 1
        assert all("PAULO" in (e.get("municipio_descricao") or "") for e in results)

    def test_todos_campos_retornados(self, api_client_e2e):
        """Resposta deve conter todos os campos do CNPJResponse."""
        r = api_client_e2e.get("/cnpj/11111111000141")
        data = r.json()
        campos_obrigatorios = [
            "cnpj_completo", "cnpj_basico", "razao_social", "uf",
            "situacao_cadastral", "cnae_fiscal", "opcao_pelo_simples",
            "opcao_pelo_mei", "run_key", "updated_at",
        ]
        for campo in campos_obrigatorios:
            assert campo in data, f"Campo '{campo}' ausente na resposta"
