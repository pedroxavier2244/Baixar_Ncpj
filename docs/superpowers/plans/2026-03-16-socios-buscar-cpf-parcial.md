# Busca de Sócios por CPF Parcial + Nome — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adicionar endpoint `GET /socios/buscar?digitos=324968&nome=JOAO+DA+SILVA` que retorna todas as empresas onde uma pessoa física aparece como sócia, usando os 6 dígitos visíveis do CPF mascarado + nome completo como chave de busca.

**Architecture:** Novo router `api/routes/socios.py` montado em `/socios` no `api/main.py`. Segue o mesmo padrão de resiliência do `cnpj.py`: helpers `_redis_get`/`_redis_set`/`_redis_incr` inline, cache primário + stale (dual-TTL), fallback stale em caso de falha do banco. Batch-fetch de sócios idêntico ao `/participacoes`.

**Tech Stack:** FastAPI, psycopg3 async, Redis (aioredis), pytest, Starlette TestClient

---

## Chunk 1: Endpoint + Testes

### Task 1: Escrever os testes que falham

**Files:**
- Modify: `tests/test_api_unit.py` — adicionar classe `TestSociosBuscarEndpoint`

- [ ] **Step 1: Adicionar classe de testes ao final de `tests/test_api_unit.py`**

```python
# ── GET /socios/buscar ────────────────────────────────────────────────────────

class TestSociosBuscarEndpoint:
    def test_buscar_retorna_empresas_da_pessoa(self, client, mock_cursor):
        """digitos + nome válidos retornam lista de CNPJWithSociosResponse."""
        from tests.conftest import _SAMPLE_EMPRESA, _SAMPLE_SOCIO
        mock_cursor.fetchall = AsyncMock(side_effect=[
            [_SAMPLE_EMPRESA],  # empresas onde a pessoa é sócia
            [_SAMPLE_SOCIO],    # sócios de cada empresa
        ])
        r = client.get("/socios/buscar?digitos=123456&nome=JOAO+DA+SILVA")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["cnpj_completo"] == "11111111000141"

    def test_buscar_sem_resultado_retorna_lista_vazia(self, client, mock_cursor):
        """Combinação sem match retorna [] (não 404)."""
        mock_cursor.fetchall = AsyncMock(return_value=[])
        r = client.get("/socios/buscar?digitos=999999&nome=PESSOA+INEXISTENTE")
        assert r.status_code == 200
        assert r.json() == []

    def test_buscar_digitos_faltando_retorna_422(self, client):
        """Parâmetro digitos ausente retorna 422 (campo obrigatório)."""
        r = client.get("/socios/buscar?nome=JOAO+DA+SILVA")
        assert r.status_code == 422

    def test_buscar_nome_faltando_retorna_422(self, client):
        """Parâmetro nome ausente retorna 422 (campo obrigatório)."""
        r = client.get("/socios/buscar?digitos=123456")
        assert r.status_code == 422

    def test_buscar_digitos_nao_numericos_retorna_400(self, client):
        """digitos com letras retorna 400."""
        r = client.get("/socios/buscar?digitos=ABCDEF&nome=JOAO+DA+SILVA")
        assert r.status_code == 400
        assert "dígitos" in r.json()["detail"].lower()

    def test_buscar_digitos_quantidade_errada_retorna_400(self, client):
        """digitos com quantidade diferente de 6 retorna 400."""
        r = client.get("/socios/buscar?digitos=123&nome=JOAO+DA+SILVA")
        assert r.status_code == 400

    def test_buscar_nome_vazio_retorna_400(self, client):
        """nome vazio retorna 400."""
        r = client.get("/socios/buscar?digitos=123456&nome=")
        assert r.status_code == 400

    def test_buscar_nome_em_minusculo_normalizado(self, client, mock_cursor):
        """nome em minúsculas deve ser normalizado para uppercase antes da query."""
        from tests.conftest import _SAMPLE_EMPRESA, _SAMPLE_SOCIO
        mock_cursor.fetchall = AsyncMock(side_effect=[
            [_SAMPLE_EMPRESA],
            [_SAMPLE_SOCIO],
        ])
        r = client.get("/socios/buscar?digitos=123456&nome=joao+da+silva")
        assert r.status_code == 200
        # Verifica que a query usou nome em uppercase
        execute_calls = mock_cursor.execute.call_args_list
        query_call = next(c for c in execute_calls if "identificador_socio" in str(c))
        assert "JOAO DA SILVA" in str(query_call)

    def test_buscar_cache_miss_salva_primary_e_stale_no_redis(self, client, mock_redis, mock_cursor):
        """Cache miss deve salvar chave primária E stale no Redis (dual-TTL)."""
        from tests.conftest import _SAMPLE_EMPRESA, _SAMPLE_SOCIO
        mock_cursor.fetchall = AsyncMock(side_effect=[
            [_SAMPLE_EMPRESA],
            [_SAMPLE_SOCIO],
        ])
        r = client.get("/socios/buscar?digitos=123456&nome=JOAO+DA+SILVA")
        assert r.status_code == 200
        # _redis_set chama setex duas vezes: chave primária + stale
        assert mock_redis.setex.call_count == 2
        keys = [c[0][0] for c in mock_redis.setex.call_args_list]
        assert any("socios_busca:" in k and not k.startswith("stale:") for k in keys)
        assert any(k.startswith("stale:socios_busca:") for k in keys)

    def test_buscar_cache_hit_nao_acessa_banco(self, mock_pool, mock_redis):
        """Cache hit deve retornar dados sem acessar o banco."""
        cached = [{"cnpj_completo": "11111111000141", "cnpj_basico": "11111111",
                   "cnpj_ordem": "0001", "cnpj_dv": "41",
                   "razao_social": "EMPRESA ALPHA LTDA", "nome_fantasia": "ALPHA STORE",
                   "uf": "SP", "situacao_cadastral": "02",
                   "run_key": "2026-01", "updated_at": "2026-01-01T00:00:00+00:00",
                   "socios": []}]
        mock_redis.get = AsyncMock(return_value=json.dumps(cached, default=str))
        with patch("api.main.psycopg_pool.AsyncConnectionPool", return_value=mock_pool), \
             patch("api.main.aioredis.from_url", return_value=mock_redis):
            with TestClient(app) as c:
                r = c.get("/socios/buscar?digitos=123456&nome=JOAO+DA+SILVA")
        assert r.status_code == 200
        assert r.json()[0]["cnpj_completo"] == "11111111000141"
        mock_pool.connection.assert_not_called()

    def test_buscar_retorna_socios_de_cada_empresa(self, client, mock_cursor):
        """Cada empresa retornada deve ter seu quadro societário preenchido."""
        from tests.conftest import _SAMPLE_EMPRESA, _SAMPLE_SOCIO
        mock_cursor.fetchall = AsyncMock(side_effect=[
            [_SAMPLE_EMPRESA],
            [_SAMPLE_SOCIO],
        ])
        r = client.get("/socios/buscar?digitos=123456&nome=JOAO+DA+SILVA")
        assert r.status_code == 200
        data = r.json()
        assert "socios" in data[0]
        assert data[0]["socios"][0]["nome_socio"] == "JOAO DA SILVA"

    def test_buscar_banco_indisponivel_sem_stale_retorna_503(self, client, mock_pool):
        """Banco indisponível sem stale → 503."""
        mock_pool.connection.side_effect = Exception("DB down")
        r = client.get("/socios/buscar?digitos=123456&nome=JOAO+DA+SILVA")
        assert r.status_code == 503

    def test_buscar_banco_indisponivel_com_stale_retorna_dados(self, mock_pool, mock_redis):
        """Banco indisponível com stale no Redis → retorna dados com X-Cache: STALE."""
        stale_data = [{"cnpj_completo": "11111111000141", "cnpj_basico": "11111111",
                       "cnpj_ordem": "0001", "cnpj_dv": "41",
                       "razao_social": "EMPRESA ALPHA LTDA", "nome_fantasia": "ALPHA STORE",
                       "uf": "SP", "situacao_cadastral": "02",
                       "run_key": "2026-01", "updated_at": "2026-01-01T00:00:00+00:00",
                       "socios": []}]
        # primary cache miss, stale cache hit
        mock_redis.get = AsyncMock(side_effect=[
            None,                                                      # chave primária: miss
            json.dumps(stale_data, default=str),                      # stale: hit
        ])
        mock_pool.connection.side_effect = Exception("DB down")
        with patch("api.main.psycopg_pool.AsyncConnectionPool", return_value=mock_pool), \
             patch("api.main.aioredis.from_url", return_value=mock_redis):
            with TestClient(app) as c:
                r = c.get("/socios/buscar?digitos=123456&nome=JOAO+DA+SILVA")
        assert r.status_code == 200
        assert r.headers.get("X-Cache") == "STALE"
        assert r.json()[0]["cnpj_completo"] == "11111111000141"

    def test_buscar_monta_cpf_mascarado_corretamente(self, client, mock_cursor):
        """A query deve usar o padrão ***XXXXXX** exato da RF."""
        from tests.conftest import _SAMPLE_EMPRESA, _SAMPLE_SOCIO
        mock_cursor.fetchall = AsyncMock(side_effect=[
            [_SAMPLE_EMPRESA],
            [_SAMPLE_SOCIO],
        ])
        client.get("/socios/buscar?digitos=324968&nome=JOAO+DA+SILVA")
        # Segundo execute é a query principal (primeiro é SET LOCAL statement_timeout)
        main_call = mock_cursor.execute.call_args_list[1]
        assert main_call[0][1] == ("***324968**", "JOAO DA SILVA")
```

- [ ] **Step 2: Rodar os testes — verificar que FALHAM com 404**

```bash
cd "C:/Users/MB NEGOCIOS/Desktop/BANCO CNPJ/Baixar_Ncpj"
python -m pytest tests/test_api_unit.py::TestSociosBuscarEndpoint -v
```

Esperado: todos falham com `404 Not Found` (rota não existe ainda).

---

### Task 2: Implementar o router e o endpoint

**Files:**
- Create: `api/routes/socios.py`
- Modify: `api/main.py` — adicionar import e registrar router

- [ ] **Step 3: Criar `api/routes/socios.py`**

```python
import hashlib
import json
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from psycopg.rows import dict_row

from api.limiter import limiter
from api.schemas import CNPJWithSociosResponse, SocioResponse
from api.security import verify_api_key
from config import settings

router = APIRouter(dependencies=[Depends(verify_api_key)])

_MV      = f"{settings.pg_serving_schema}.mv_cnpj_full"
_SOCIOS  = f"{settings.pg_schema}.rf_socios"
_TIMEOUT = f"SET LOCAL statement_timeout = {settings.db_query_timeout_ms}"


async def _redis_get(redis, key: str) -> Optional[str]:
    try:
        return await redis.get(key)
    except Exception:
        return None


async def _redis_set(redis, key: str, value: str) -> None:
    """Grava chave primária (TTL busca) e stale (90d) com degradação graceful."""
    try:
        await redis.setex(key, settings.search_cache_ttl, value)
        await redis.setex(f"stale:{key}", settings.cache_stale_ttl, value)
    except Exception:
        pass


async def _redis_incr(redis, key: str) -> None:
    try:
        await redis.incr(key)
    except Exception:
        pass


@router.get("/buscar", response_model=list[CNPJWithSociosResponse])
@limiter.limit(settings.rate_limit_search)
async def buscar_por_cpf_parcial(
    request: Request,
    response: Response,
    digitos: str,
    nome: str,
):
    """
    Retorna todas as empresas onde uma pessoa física aparece como sócia.

    Usa os 6 dígitos visíveis do CPF mascarado (formato RF: ***XXXXXX**)
    combinados com o nome completo do sócio para identificar a pessoa.
    A combinação é praticamente única na base da Receita Federal.
    """
    if not digitos.isdigit() or len(digitos) != 6:
        raise HTTPException(
            status_code=400,
            detail="digitos deve conter exatamente 6 dígitos numéricos"
        )
    if not nome.strip():
        raise HTTPException(status_code=400, detail="nome não pode ser vazio")

    nome_upper = nome.strip().upper()
    cpf_mascarado = f"***{digitos}**"

    cache_key = "socios_busca:" + hashlib.md5(
        f"{cpf_mascarado}|{nome_upper}".encode()
    ).hexdigest()

    cached = await _redis_get(request.app.state.redis, cache_key)
    if cached:
        await _redis_incr(request.app.state.redis, "stats:cache_hits")
        return [CNPJWithSociosResponse(**r) for r in json.loads(cached)]

    await _redis_incr(request.app.state.redis, "stats:cache_misses")
    try:
        async with request.app.state.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(_TIMEOUT)
                await cur.execute(
                    f"""
                    SELECT DISTINCT mv.*
                    FROM {_MV} mv
                    INNER JOIN {_SOCIOS} s ON s.cnpj_basico = mv.cnpj_basico
                    WHERE s.identificador_socio = '2'
                      AND s.cnpj_cpf_socio = %s
                      AND s.nome_socio = %s
                    ORDER BY mv.razao_social
                    LIMIT 100
                    """,
                    (cpf_mascarado, nome_upper),
                )
                rows = await cur.fetchall()

                if not rows:
                    return []

                cnpj_basicos = list({r["cnpj_basico"] for r in rows})
                placeholders = ", ".join("%s" for _ in cnpj_basicos)
                await cur.execute(
                    f"SELECT * FROM {_SOCIOS} "
                    f"WHERE cnpj_basico IN ({placeholders}) "
                    f"ORDER BY cnpj_basico, nome_socio",
                    cnpj_basicos,
                )
                socios_rows = await cur.fetchall()
    except Exception as exc:
        stale = await _redis_get(request.app.state.redis, f"stale:{cache_key}")
        if stale:
            response.headers["X-Cache"] = "STALE"
            return [CNPJWithSociosResponse(**r) for r in json.loads(stale)]
        detail = (
            "Banco de dados sobrecarregado. Tente novamente."
            if "statement timeout" in str(exc).lower()
            else "Erro ao consultar banco de dados."
        )
        raise HTTPException(status_code=503, detail=detail)

    socios_by_cnpj: dict[str, list] = defaultdict(list)
    for s in socios_rows:
        socios_by_cnpj[s["cnpj_basico"].strip()].append(SocioResponse(**s))

    result = [
        CNPJWithSociosResponse(
            **r,
            socios=socios_by_cnpj.get(r["cnpj_basico"].strip(), []),
        )
        for r in rows
    ]

    await _redis_set(
        request.app.state.redis,
        cache_key,
        json.dumps([r.model_dump() for r in result], default=str),
    )

    return result
```

- [ ] **Step 4: Atualizar `api/main.py`**

Adicionar o import junto com os outros imports de rotas (bloco existente linhas 21-24):
```python
from api.routes import socios as socios_router
```

Adicionar o router após o `search_router` (linha ~71):
```python
app.include_router(socios_router.router, prefix="/socios", tags=["Sócios"])
```

- [ ] **Step 5: Rodar os testes — verificar que PASSAM**

```bash
python -m pytest tests/test_api_unit.py::TestSociosBuscarEndpoint -v
```

Esperado: todos os 14 testes passam.

- [ ] **Step 6: Rodar a suíte completa — verificar sem regressão**

```bash
python -m pytest tests/test_api_unit.py -v
```

Esperado: todos os testes passam.

- [ ] **Step 7: Commit**

```bash
git add api/routes/socios.py api/main.py tests/test_api_unit.py
git commit -m "feat(api): endpoint GET /socios/buscar — busca por CPF parcial + nome"
```

---

## Notas de implementação

**Por que igualdade exata e não LIKE:**
O formato `***XXXXXX**` é fixo na RF — sempre 3 asteriscos + 6 dígitos + 2 asteriscos. Igualdade exata permite uso de índice B-tree, muito mais rápido que LIKE em 27M linhas.

**Nome em uppercase:**
A RF armazena nomes em maiúsculas. O endpoint normaliza com `.upper()` antes de comparar, aceitando qualquer capitalização do cliente.

**Índice recomendado para produção:**
Com 27M linhas em `rf_socios`, a query sem índice fará seq scan. Adicionar via psql após o próximo ETL:
```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_socios_cpf_nome
ON cnpj.rf_socios (cnpj_cpf_socio, nome_socio)
WHERE identificador_socio = '2';
```
`CONCURRENTLY` não bloqueia leituras. Libera ~segundos de latência para ~milissegundos.
