# Design: Sócios no Endpoint de Search

**Data:** 2026-03-13
**Status:** Aprovado

## Problema

O endpoint `GET /search` retorna dados das empresas mas não inclui o quadro societário. O usuário precisa fazer chamadas separadas para cada empresa para obter os sócios, o que é inviável quando há múltiplos resultados.

## Solução

Incluir `socios` em cada resultado do search via query em lote (batch).

## Abordagem: Batch Query

Após buscar as empresas, coletar todos os `cnpj_basico` e fazer **uma única query** `WHERE cnpj_basico IN (...)` para obter todos os sócios. Agrupar por `cnpj_basico` e anexar a cada empresa.

**Fluxo:**
1. Executar query de search (existente)
2. Se não houver resultados, retornar `[]` imediatamente (sem query de sócios)
3. Coletar `cnpj_basico` dos resultados
4. Executar batch: `SELECT * FROM cnpj.rf_socios WHERE cnpj_basico IN (...) ORDER BY cnpj_basico, nome_socio`
5. Agrupar sócios por `cnpj_basico` em dict (`defaultdict(list)`)
6. Montar `CNPJWithSociosResponse` para cada empresa com `socios=grupo.get(cnpj_basico, [])`
   - CNPJs sem sócios cadastrados recebem `socios: []` — não gera erro
7. Cachear resultado completo (TTL 5 min)

**Ambas as queries devem executar dentro do mesmo bloco `async with pool.connection()`**, com um único `SET LOCAL statement_timeout`. Isso evita pressão dupla no pool de conexões.

## Mudanças

### `api/schemas.py`
Adicionar `CNPJWithSociosResponse` estendendo `CNPJResponse`:
```python
class CNPJWithSociosResponse(CNPJResponse):
    socios: list[SocioResponse] = []
```

### `api/routes/search.py`

**Constante nova** (module level, igual ao padrão de `cnpj.py`):
```python
_SOCIOS = f"{settings.pg_schema}.rf_socios"
```

**Response model:** trocar `list[CNPJResponse]` por `list[CNPJWithSociosResponse]`.

**Cache — leitura:** usar `CNPJWithSociosResponse` (não `CNPJResponse`) ao deserializar do cache:
```python
return [CNPJWithSociosResponse(**r) for r in json.loads(cached)]
```

**Cache — chave:** usar prefixo `search_v2:` em vez de `search:` para evitar colisão com entradas antigas do cache que não contêm `socios`. Isso garante que deploys não sirvam dados misturados.

**Tratamento de erros:** a query de sócios deve ser envolvida no mesmo `try/except` da query principal, com `HTTPException(503)` em timeout — igual ao padrão de `cnpj.py`.

**Tamanho do IN:** a cláusula `IN (...)` é limitada pelo `limit` máximo do search (atualmente 100). Se o limite máximo for aumentado no futuro, reavaliar performance do batch.

## O que NÃO muda
- `GET /cnpj/{cnpj}` — continua sem sócios embutidos
- `GET /cnpj/{cnpj}/socios` — sem alteração
- `CNPJResponse` — sem alteração
- Rate limit, autenticação, timeout — sem alteração

## Performance
- Search sem sócios: 1 query
- Search com sócios: 2 queries dentro de 1 conexão (independente do número de resultados)
- Resultados cacheados por 5 min — batch só executa em cache miss
