# Cache TTL 30d + Stale Fallback + Hit Rate Metrics

**Data:** 2026-03-13
**Status:** Aprovado

## Problema

TTL atual de 24h força ~90% das consultas a ir ao banco todo dia, mesmo sendo CNPJs da mesma carteira. Sem fallback quando o banco falha. Sem visibilidade de eficiência do cache.

## Solução

Duas chaves Redis por CNPJ + contadores persistentes.

## Fluxo

```
GET /cnpj/{cnpj}:

  1. Busca cnpj:{key} (TTL 30d)
     ├─ HIT  → INCR stats:cache_hits  → retorna dado
     └─ MISS → INCR stats:cache_misses → consulta DB
                    ├─ DB ok  → setex cnpj:{key} 30d + setex stale:cnpj:{key} 90d → retorna
                    └─ DB off → busca stale:cnpj:{key}
                                   ├─ HIT  → retorna com header X-Cache: STALE
                                   └─ MISS → 503
```

## Chaves Redis

| Chave | TTL | Uso |
|---|---|---|
| `cnpj:{cnpj_clean}` | 30d (2.592.000s) | Caminho normal |
| `stale:cnpj:{cnpj_clean}` | 90d (7.776.000s) | Fallback quando DB falha |
| `socios:{cnpj_basico}` | 30d | Caminho normal sócios |
| `stale:socios:{cnpj_basico}` | 90d | Fallback sócios |
| `stats:cache_hits` | sem TTL | Contador persistente |
| `stats:cache_misses` | sem TTL | Contador persistente |

`/search` mantém TTL 5min sem stale — resultados de busca mudam com frequência.

## Métricas no /health

```json
{
  "cache_hits": 8423,
  "cache_misses": 312,
  "hit_rate": 0.964
}
```

`hit_rate = null` enquanto não houver consultas.

## Arquivos alterados

| Arquivo | Mudança |
|---|---|
| `config.py` | `cache_ttl` 86400→2592000, novo `cache_stale_ttl=7776000` |
| `api/routes/cnpj.py` | `_redis_set` grava stale; fallback stale no except; `_redis_incr` para contadores |
| `api/routes/health.py` | Lê contadores, calcula hit_rate, adiciona ao response |
| `api/schemas.py` | `cache_hits`, `cache_misses`, `hit_rate` no `HealthResponse` |
