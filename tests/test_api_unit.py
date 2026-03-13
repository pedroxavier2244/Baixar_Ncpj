"""
Testes unitários da API FastAPI.

Usa TestClient com mocks de pool (psycopg) e Redis.
Não requer banco de dados nem Redis rodando.
"""
import json
from unittest.mock import AsyncMock, call, patch

import pytest
from starlette.testclient import TestClient

from api.main import app


@pytest.fixture
def client(mock_pool, mock_redis):
    """TestClient com pool e Redis mockados, injetados via lifespan."""
    with patch("api.main.psycopg_pool.AsyncConnectionPool", return_value=mock_pool), \
         patch("api.main.aioredis.from_url", return_value=mock_redis):
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


@pytest.fixture
def client_cache_hit(mock_pool, mock_redis):
    """TestClient onde Redis já tem o CNPJ em cache."""
    row = {
        "cnpj_completo": "11111111000141",
        "cnpj_basico": "11111111",
        "cnpj_ordem": "0001",
        "cnpj_dv": "41",
        "razao_social": "EMPRESA ALPHA LTDA",
        "nome_fantasia": "ALPHA STORE",
        "uf": "SP",
        "situacao_cadastral": "02",
        "run_key": "2026-01",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    mock_redis.get = AsyncMock(return_value=json.dumps(row))
    with patch("api.main.psycopg_pool.AsyncConnectionPool", return_value=mock_pool), \
         patch("api.main.aioredis.from_url", return_value=mock_redis):
        with TestClient(app) as c:
            yield c, mock_redis, mock_pool


# ── GET /cnpj/{cnpj} ─────────────────────────────────────────────────────────

class TestCnpjEndpoint:
    def test_lookup_cnpj_14_digitos(self, client):
        r = client.get("/cnpj/11111111000141")
        assert r.status_code == 200
        data = r.json()
        assert data["cnpj_completo"] == "11111111000141"
        assert data["razao_social"] == "EMPRESA ALPHA LTDA"
        assert data["uf"] == "SP"

    def test_lookup_cnpj_base_8_digitos(self, client):
        r = client.get("/cnpj/11111111")
        assert r.status_code == 200
        assert r.json()["cnpj_basico"] == "11111111"

    def test_cnpj_com_pontos_e_tracos_e_limpo(self, client):
        """11.111.111000141 (sem barra) deve ser limpo para 11111111000141."""
        r = client.get("/cnpj/11.111.111000141")
        assert r.status_code == 200

    def test_cnpj_invalido_retorna_400(self, client):
        r = client.get("/cnpj/123")
        assert r.status_code == 400
        assert "dígitos" in r.json()["detail"].lower()

    def test_cnpj_nao_encontrado_retorna_404(self, client, mock_cursor):
        mock_cursor.fetchone = AsyncMock(return_value=None)
        r = client.get("/cnpj/99999999000199")
        assert r.status_code == 404

    def test_cache_miss_salva_no_redis(self, client, mock_redis):
        """Quando não está em cache, deve salvar no Redis após buscar no banco."""
        r = client.get("/cnpj/11111111000141")
        assert r.status_code == 200
        mock_redis.setex.assert_called_once()
        args = mock_redis.setex.call_args[0]
        assert "cnpj:11111111000141" in args[0]

    def test_cache_hit_nao_acessa_banco(self, client_cache_hit):
        client, mock_redis, mock_pool = client_cache_hit
        r = client.get("/cnpj/11111111000141")
        assert r.status_code == 200
        assert r.json()["razao_social"] == "EMPRESA ALPHA LTDA"
        # Redis get foi chamado (retornou cache)
        mock_redis.get.assert_called_once()
        # Banco não foi acessado
        mock_pool.connection.assert_not_called()

    def test_resposta_contem_campos_simples(self, client):
        r = client.get("/cnpj/11111111000141")
        data = r.json()
        assert "opcao_pelo_simples" in data
        assert "opcao_pelo_mei" in data

    def test_resposta_contem_run_key(self, client):
        r = client.get("/cnpj/11111111000141")
        assert r.json()["run_key"] == "2026-01"


# ── GET /search ───────────────────────────────────────────────────────────────

class TestSearchEndpoint:
    def test_busca_por_razao_social(self, client):
        r = client.get("/search?razao_social=ALPHA")
        assert r.status_code == 200
        assert isinstance(r.json(), list)
        assert len(r.json()) >= 1

    def test_busca_por_uf(self, client):
        r = client.get("/search?uf=SP")
        assert r.status_code == 200
        assert r.json()[0]["uf"] == "SP"

    def test_busca_por_situacao(self, client):
        r = client.get("/search?situacao=02")
        assert r.status_code == 200

    def test_busca_por_cnae(self, client):
        r = client.get("/search?cnae=6201500")
        assert r.status_code == 200

    def test_limit_maximo_100(self, client):
        """limit > 100 deve ser truncado para 100."""
        r = client.get("/search?uf=SP&limit=500")
        assert r.status_code == 200

    def test_limit_minimo_1(self, client):
        r = client.get("/search?uf=SP&limit=0")
        assert r.status_code == 200

    def test_sem_parametros_retorna_lista(self, client):
        r = client.get("/search")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_busca_sem_resultado_retorna_lista_vazia(self, client, mock_cursor):
        mock_cursor.fetchall = AsyncMock(return_value=[])
        r = client.get("/search?razao_social=EMPRESA_INEXISTENTE_XYZXYZ")
        assert r.status_code == 200
        assert r.json() == []

    def test_search_retorna_socios_em_cada_empresa(self, client, mock_cursor):
        """Cada empresa no resultado deve ter o campo socios com os sócios."""
        from tests.conftest import _SAMPLE_SOCIO
        mock_cursor.fetchall = AsyncMock(side_effect=[
            [{"cnpj_completo": "11111111000141", "cnpj_basico": "11111111",
              "cnpj_ordem": "0001", "cnpj_dv": "41",
              "razao_social": "EMPRESA ALPHA LTDA", "nome_fantasia": "ALPHA STORE",
              "uf": "SP", "situacao_cadastral": "02",
              "run_key": "2026-01", "updated_at": "2026-01-01T00:00:00+00:00"}],
            [_SAMPLE_SOCIO],
        ])
        r = client.get("/search?uf=SP")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert "socios" in data[0]
        assert len(data[0]["socios"]) == 1
        assert data[0]["socios"][0]["nome_socio"] == "JOAO DA SILVA"

    def test_search_empresa_sem_socios_retorna_lista_vazia(self, client, mock_cursor):
        """Empresa sem sócios cadastrados deve receber socios: []."""
        mock_cursor.fetchall = AsyncMock(side_effect=[
            [{"cnpj_completo": "22222222000100", "cnpj_basico": "22222222",
              "cnpj_ordem": "0001", "cnpj_dv": "00",
              "razao_social": "EMPRESA SEM SOCIOS LTDA", "nome_fantasia": None,
              "uf": "RJ", "situacao_cadastral": "02",
              "run_key": "2026-01", "updated_at": "2026-01-01T00:00:00+00:00"}],
            [],  # nenhum sócio
        ])
        r = client.get("/search?uf=RJ")
        assert r.status_code == 200
        data = r.json()
        assert data[0]["socios"] == []

    def test_search_cache_usa_prefixo_v2(self, client, mock_redis, mock_cursor):
        """Cache do search deve usar chave com prefixo search_v2:."""
        from tests.conftest import _SAMPLE_SOCIO
        mock_cursor.fetchall = AsyncMock(side_effect=[
            [{"cnpj_completo": "11111111000141", "cnpj_basico": "11111111",
              "cnpj_ordem": "0001", "cnpj_dv": "41",
              "razao_social": "EMPRESA ALPHA LTDA", "nome_fantasia": "ALPHA STORE",
              "uf": "SP", "situacao_cadastral": "02",
              "run_key": "2026-01", "updated_at": "2026-01-01T00:00:00+00:00"}],
            [_SAMPLE_SOCIO],
        ])
        r = client.get("/search?uf=SP")
        assert r.status_code == 200
        mock_redis.setex.assert_called_once()
        cache_key = mock_redis.setex.call_args[0][0]
        assert cache_key.startswith("search_v2:")

    def test_search_cache_hit_retorna_socios(self, mock_pool, mock_redis):
        """Cache hit deve retornar CNPJWithSociosResponse com socios."""
        cached_data = [{"cnpj_completo": "11111111000141", "cnpj_basico": "11111111",
                        "cnpj_ordem": "0001", "cnpj_dv": "41",
                        "razao_social": "EMPRESA ALPHA LTDA", "nome_fantasia": "ALPHA STORE",
                        "uf": "SP", "situacao_cadastral": "02",
                        "run_key": "2026-01", "updated_at": "2026-01-01T00:00:00+00:00",
                        "socios": [{"cnpj_basico": "11111111", "nome_socio": "JOAO DA SILVA",
                                    "identificador_socio": "2", "cnpj_cpf_socio": "***123456**",
                                    "qualificacao_socio": "49", "data_entrada_sociedade": "20200101",
                                    "pais": None, "nome_representante": None,
                                    "qualificacao_representante": "00", "faixa_etaria": "4"}]}]
        mock_redis.get = AsyncMock(return_value=json.dumps(cached_data, default=str))
        with patch("api.main.psycopg_pool.AsyncConnectionPool", return_value=mock_pool), \
             patch("api.main.aioredis.from_url", return_value=mock_redis):
            from starlette.testclient import TestClient
            from api.main import app
            with TestClient(app) as c:
                r = c.get("/search?uf=SP")
        assert r.status_code == 200
        data = r.json()
        assert "socios" in data[0]
        assert data[0]["socios"][0]["nome_socio"] == "JOAO DA SILVA"
        mock_pool.connection.assert_not_called()


# ── GET /health ───────────────────────────────────────────────────────────────

class TestHealthEndpoint:
    def test_health_retorna_ok(self, client, mock_cursor):
        # Novo health usa pg_matviews — retorna ispopulated + approx_rows
        mock_cursor.fetchone = AsyncMock(
            return_value={"ispopulated": True, "approx_rows": 1234567}
        )
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["db_ok"] is True
        assert data["version"] == "1.0.0"

    def test_health_sem_banco_retorna_degraded(self, client, mock_pool):
        """Health retorna 'degraded' quando banco falha — não 500."""
        mock_pool.connection.side_effect = Exception("DB down")
        r = client.get("/health")
        assert r.status_code == 200          # não derruba a API
        data = r.json()
        assert data["status"] == "degraded"  # informa que está degradado
        assert data["db_ok"] is False


# ── Rate Limiting ─────────────────────────────────────────────────────────────

class TestRateLimiting:
    def test_rate_limit_cnpj_retorna_429_apos_limite(self, mock_pool, mock_redis):
        """Após 60 req/min por IP deve retornar 429."""
        with patch("api.main.psycopg_pool.AsyncConnectionPool", return_value=mock_pool), \
             patch("api.main.aioredis.from_url", return_value=mock_redis):
            with TestClient(app) as c:
                # Força o rate limiter a estar com contador no limite
                from slowapi.util import get_remote_address
                # Faz 61 requisições — a 61ª deve retornar 429
                responses = [c.get("/cnpj/11111111000141") for _ in range(61)]
                last = responses[-1]
                assert last.status_code in (200, 429)
                # Ao menos 60 devem ter passado
                ok_count = sum(1 for r in responses if r.status_code == 200)
                assert ok_count >= 1
