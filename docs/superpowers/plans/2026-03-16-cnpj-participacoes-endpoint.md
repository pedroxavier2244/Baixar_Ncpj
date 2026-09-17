# CNPJ Participações Endpoint — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adicionar endpoint `GET /cnpj/{cnpj}/participacoes` que retorna todas as empresas onde um CNPJ aparece como sócio (PJ).

**Architecture:** Novo endpoint no router existente `api/routes/cnpj.py`, reutilizando as constantes `_MV`, `_SOCIOS`, helpers de cache e o padrão de batch-fetch de sócios idêntico ao `/search`. Testes unitários no padrão existente em `tests/test_api_unit.py` com mocks de pool e Redis.

**Tech Stack:** FastAPI, psycopg3 async, Redis (aioredis), pytest, Starlette TestClient

---

## Chunk 1: Endpoint + Testes

### Task 1: Escrever os testes que falham

**Files:**
- Modify: `tests/conftest.py` — adicionar `_SAMPLE_PJ_SOCIO`
- Modify: `tests/test_api_unit.py` — adicionar classe `TestParticipacoesEndpoint`

- [ ] **Step 1: Adicionar dado de PJ socio no conftest**

Em `tests/conftest.py`, logo após `_SAMPLE_SOCIO` (linha ~50), adicionar:

```python
_SAMPLE_PJ_SOCIO = {
    "cnpj_basico": "11111111",
    "identificador_socio": "1",          # 1 = PJ
    "nome_socio": "EMPRESA BETA LTDA",
    "cnpj_cpf_socio": "22222222000100",  # CNPJ da empresa sócia
    "qualificacao_socio": "05",
    "data_entrada_sociedade": "20210101",
    "pais": None,
    "nome_representante": None,
    "qualificacao_representante": "00",
    "faixa_etaria": None,
}
```

- [ ] **Step 2: Adicionar classe de testes no test_api_unit.py**

Ao final de `tests/test_api_unit.py`, adicionar:

```python
# ── GET /cnpj/{cnpj}/participacoes ────────────────────────────────────────────

class TestParticipacoesEndpoint:
    def test_participacoes_retorna_lista_de_empresas(self, client, mock_cursor):
        """CNPJ válido com participações retorna lista de CNPJWithSociosResponse."""
        from tests.conftest import _SAMPLE_EMPRESA, _SAMPLE_PJ_SOCIO
        mock_cursor.fetchall = AsyncMock(side_effect=[
            [_SAMPLE_EMPRESA],   # empresas onde aparece como sócio
            [_SAMPLE_PJ_SOCIO],  # sócios dessas empresas
        ])
        r = client.get("/cnpj/22222222000100/participacoes")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["cnpj_completo"] == "11111111000141"

    def test_participacoes_sem_resultado_retorna_lista_vazia(self, client, mock_cursor):
        """CNPJ sem participações retorna [] (não 404)."""
        mock_cursor.fetchall = AsyncMock(return_value=[])
        r = client.get("/cnpj/99999999000199/participacoes")
        assert r.status_code == 200
        assert r.json() == []

    def test_participacoes_cnpj_invalido_retorna_400(self, client):
        """CNPJ com menos de 14 dígitos retorna 400."""
        r = client.get("/cnpj/1234/participacoes")
        assert r.status_code == 400
        assert "14" in r.json()["detail"]

    def test_participacoes_cnpj_8_digitos_retorna_400(self, client):
        """CNPJ base (8 dígitos) não é válido para participações — exige 14."""
        r = client.get("/cnpj/22222222/participacoes")
        assert r.status_code == 400

    def test_participacoes_cache_miss_salva_no_redis(self, client, mock_redis, mock_cursor):
        """Cache miss deve salvar resultado no Redis."""
        from tests.conftest import _SAMPLE_EMPRESA, _SAMPLE_PJ_SOCIO
        mock_cursor.fetchall = AsyncMock(side_effect=[
            [_SAMPLE_EMPRESA],
            [_SAMPLE_PJ_SOCIO],
        ])
        r = client.get("/cnpj/22222222000100/participacoes")
        assert r.status_code == 200
        mock_redis.setex.assert_called()
        # Verifica que a chave de cache usa prefixo correto
        first_call_key = mock_redis.setex.call_args_list[0][0][0]
        assert "participacoes:22222222000100" in first_call_key

    def test_participacoes_cache_hit_nao_acessa_banco(self, mock_pool, mock_redis):
        """Cache hit deve retornar dados sem acessar o banco."""
        from tests.conftest import _SAMPLE_EMPRESA
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
                r = c.get("/cnpj/22222222000100/participacoes")
        assert r.status_code == 200
        assert r.json()[0]["cnpj_completo"] == "11111111000141"
        mock_pool.connection.assert_not_called()

    def test_participacoes_retorna_socios_de_cada_empresa(self, client, mock_cursor):
        """Cada empresa retornada deve ter seu quadro societário preenchido."""
        from tests.conftest import _SAMPLE_EMPRESA, _SAMPLE_PJ_SOCIO
        mock_cursor.fetchall = AsyncMock(side_effect=[
            [_SAMPLE_EMPRESA],
            [_SAMPLE_PJ_SOCIO],
        ])
        r = client.get("/cnpj/22222222000100/participacoes")
        assert r.status_code == 200
        data = r.json()
        assert "socios" in data[0]
        assert len(data[0]["socios"]) == 1
        assert data[0]["socios"][0]["nome_socio"] == "EMPRESA BETA LTDA"

    def test_participacoes_banco_indisponivel_retorna_503(self, client, mock_pool):
        """Banco indisponível sem stale → 503."""
        mock_pool.connection.side_effect = Exception("DB down")
        r = client.get("/cnpj/22222222000100/participacoes")
        assert r.status_code == 503
```

- [ ] **Step 3: Rodar os testes — verificar que FALHAM com 404**

```bash
cd "C:/Users/MB NEGOCIOS/Desktop/BANCO CNPJ/Baixar_Ncpj"
python -m pytest tests/test_api_unit.py::TestParticipacoesEndpoint -v
```

Esperado: todos os testes falham com `404 Not Found` (rota não existe ainda).

---

### Task 2: Implementar o endpoint

**Files:**
- Modify: `api/routes/cnpj.py` — adicionar import, `defaultdict` no topo e endpoint `GET /{cnpj}/participacoes`

- [ ] **Step 4a: Atualizar imports no topo de `api/routes/cnpj.py`**

Linha 8 atual:
```python
from api.schemas import CNPJResponse, SocioResponse
```
Alterar para:
```python
from collections import defaultdict

from api.schemas import CNPJResponse, CNPJWithSociosResponse, SocioResponse
```

- [ ] **Step 4b: Adicionar endpoint no cnpj.py**

Em `api/routes/cnpj.py`, após o endpoint `get_socios` (linha ~149), adicionar:

```python
@router.get("/{cnpj}/participacoes", response_model=list[CNPJWithSociosResponse])
@limiter.limit(settings.rate_limit_cnpj)
async def get_participacoes(cnpj: str, request: Request, response: Response):
    """
    Retorna todas as empresas onde este CNPJ aparece como sócio (PJ).
    Útil para expandir nós de empresa no grafo de vínculos societários.
    Requer CNPJ completo (14 dígitos).
    """
    cnpj_clean = "".join(c for c in cnpj if c.isdigit())
    if len(cnpj_clean) != 14:
        raise HTTPException(status_code=400, detail="CNPJ deve ter 14 dígitos para busca de participações")

    cache_key = f"participacoes:{cnpj_clean}"

    cached = await _redis_get(request.app.state.redis, cache_key)
    if cached:
        await _redis_incr(request.app.state.redis, "stats:cache_hits")
        return [CNPJWithSociosResponse(**r) for r in json.loads(cached)]

    await _redis_incr(request.app.state.redis, "stats:cache_misses")
    try:
        async with request.app.state.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(_TIMEOUT)
                # Busca empresas onde este CNPJ é sócio PJ
                await cur.execute(
                    f"""
                    SELECT DISTINCT mv.*
                    FROM {_MV} mv
                    INNER JOIN {_SOCIOS} s ON s.cnpj_basico = mv.cnpj_basico
                    WHERE s.identificador_socio = '1'
                      AND s.cnpj_cpf_socio = %s
                    ORDER BY mv.razao_social
                    LIMIT 100
                    """,
                    (cnpj_clean,),
                )
                # Nota: DISTINCT deduplica por todos os campos de mv.*
                # Se uma empresa tiver múltiplos registros duplicados em rf_socios
                # para o mesmo CNPJ sócio, o JOIN pode gerar duplicatas — DISTINCT resolve.
                rows = await cur.fetchall()

                if not rows:
                    return []

                # Batch fetch de sócios para cada empresa encontrada
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

- [ ] **Step 5: Adicionar teste para CNPJ com formatação**

Adicionar na classe `TestParticipacoesEndpoint` em `tests/test_api_unit.py`:

```python
    def test_participacoes_cnpj_formatado_aceito(self, client, mock_cursor):
        """CNPJ formatado (22.222.222/0001-00) deve ser aceito após strip de não-dígitos."""
        mock_cursor.fetchall = AsyncMock(return_value=[])
        r = client.get("/cnpj/22.222.222%2F0001-00/participacoes")
        # 22.222.222/0001-00 → 22222222000100 = 14 dígitos → aceito
        assert r.status_code == 200
        assert r.json() == []
```

- [ ] **Step 6: Rodar os testes — verificar que PASSAM**

```bash
cd "C:/Users/MB NEGOCIOS/Desktop/BANCO CNPJ/Baixar_Ncpj"
python -m pytest tests/test_api_unit.py::TestParticipacoesEndpoint -v
```

Esperado: todos os 9 testes passam.

- [ ] **Step 7: Rodar toda a suíte para verificar regressão**

```bash
python -m pytest tests/test_api_unit.py -v
```

Esperado: todos os testes passam (sem regressão).

- [ ] **Step 8: Commit**

```bash
git add api/routes/cnpj.py tests/test_api_unit.py tests/conftest.py
git commit -m "feat(api): endpoint GET /cnpj/{cnpj}/participacoes com cache e testes"
```

---

## Notas de implementação

**Sobre `cnpj_cpf_socio` para PJ:**
No arquivo QSA da Receita Federal, o campo "CNPJ do sócio" para pessoas jurídicas é armazenado como CNPJ completo (14 dígitos). Se os testes de integração mostrarem resultados vazios, verificar como o ETL armazena esse campo executando:
```sql
SELECT cnpj_cpf_socio, length(cnpj_cpf_socio)
FROM cnpj.rf_socios
WHERE identificador_socio = '1'
LIMIT 5;
```
Se armazena 8 dígitos (basico), ajustar o WHERE para `cnpj_cpf_socio = %s` com `cnpj_clean[:8]`.

**Limit de 100 resultados:**
Uma holding grande pode aparecer como sócia em centenas de empresas. O LIMIT 100 protege a performance. Pode ser exposto como query param no futuro se necessário.
