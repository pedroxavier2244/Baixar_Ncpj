# Documentação — Cache de CNPJs Consultados

**Data:** 2026-03-13
**Revisão:** Time de Infraestrutura
**Status:** Publicado

---

## O QUE É?

Como API CNPJ,
Quero armazenar em cache os CNPJs já consultados,
Para que consultas repetidas sejam instantâneas e o sistema funcione mesmo se a VPS tiver instabilidade.

**Contexto:** 90%+ das consultas são CNPJs repetidos (mesma carteira de clientes). Cache reduz latência e cria fallback natural contra instabilidades.

---

## Critérios de Aceitação

| Critério | Status |
|---|---|
| Cache com TTL configurável (sugestão: 30 dias) | ✅ APROVADO |
| Fallback: se API principal falha, serve do cache | ✅ APROVADO |
| Métrica: hit rate do cache | ✅ APROVADO |

---

## Critério 1 — Cache com TTL configurável (30 dias)

**Implementação:** Cada CNPJ consultado é armazenado no Redis com TTL de 30 dias.

| Parâmetro | Valor |
|---|---|
| Config | `cache_ttl` em `config.py` |
| Valor atual | 2.592.000 segundos (30 dias) |
| Chave Redis | `cnpj:{cnpj_clean}` |
| Chave sócios | `socios:{cnpj_basico}` |

**Comportamento:** Primeira consulta a um CNPJ vai ao banco (PostgreSQL) e armazena no Redis. Todas as consultas seguintes dentro de 30 dias são respondidas direto do Redis, sem tocar no banco.

**Resultado:** ✅ APROVADO — TTL configurável via `config.py` ou variável de ambiente `CACHE_TTL`.

---

## Critério 2 — Fallback quando API principal falha

**Implementação:** Duas chaves Redis por CNPJ — primária (30d) e stale (90d). Se o banco de dados ficar indisponível e a chave primária tiver expirado, a API serve o dado stale com header `X-Cache: STALE`.

**Fluxo completo:**

```
GET /cnpj/{cnpj}:

  1. Busca chave primária  cnpj:{key}  (TTL 30d)
     ├─ HIT  → retorna dado (instantâneo)
     └─ MISS → consulta banco
                    ├─ Banco ok  → salva primária (30d) + stale (90d) → retorna
                    └─ Banco off → busca chave stale:cnpj:{key} (TTL 90d)
                                      ├─ HIT  → retorna dado com header X-Cache: STALE
                                      └─ MISS → 503
```

| Chave Redis | TTL | Uso |
|---|---|---|
| `cnpj:{cnpj_clean}` | 30 dias | Caminho normal |
| `stale:cnpj:{cnpj_clean}` | 90 dias | Fallback quando banco falha |
| `socios:{cnpj_basico}` | 30 dias | Caminho normal sócios |
| `stale:socios:{cnpj_basico}` | 90 dias | Fallback sócios |

**Resultado:** ✅ APROVADO — API serve dados mesmo com banco indisponível, enquanto houver dado stale (até 90 dias após última consulta ao banco).

---

## Critério 3 — Métrica: hit rate do cache

**Implementação:** Contadores persistentes no Redis (`stats:cache_hits`, `stats:cache_misses`). Expostos no endpoint `GET /health`.

**Endpoint:** `GET /health`

```json
{
    "status": "ok",
    "cache_ok": true,
    "cache_hits": 2,
    "cache_misses": 0,
    "hit_rate": 1.0
}
```

| Campo | Descrição |
|---|---|
| `cache_hits` | Total de consultas servidas pelo Redis |
| `cache_misses` | Total de consultas que foram ao banco |
| `hit_rate` | Proporção de hits — `hits / (hits + misses)` |

`hit_rate: 1.0` = 100% das consultas servidas do cache.
`hit_rate: null` = nenhuma consulta realizada ainda.

Os contadores persistem entre reinicios do container (armazenados no Redis, não em memória).

**Resultado:** ✅ APROVADO — hit rate visível em tempo real via `/health`.

---

## Validação em Produção

```json
{
    "status": "ok",
    "version": "1.0.0",
    "last_success_run_key": "2026-02",
    "mv_row_count": 69846072,
    "db_ok": true,
    "cache_ok": true,
    "cache_hits": 2,
    "cache_misses": 0,
    "hit_rate": 1.0
}
```

Validado em produção em 2026-03-13 no ambiente `http://5.189.163.33`.

---

## Arquivos Alterados

| Arquivo | Mudança |
|---|---|
| `config.py` | `cache_ttl` 24h → 30 dias; novo `cache_stale_ttl` = 90 dias |
| `api/routes/cnpj.py` | Escrita dupla (primária + stale); fallback stale quando banco falha; contadores de hit/miss |
| `api/routes/health.py` | Lê contadores Redis, calcula `hit_rate`, inclui no response |
| `api/schemas.py` | Campos `cache_hits`, `cache_misses`, `hit_rate` no `HealthResponse` |

---

## Revisão Técnica

| Item | Status |
|---|---|
| Revisado por | Time de Infraestrutura |
| Data da revisão | 2026-03-13 |
| Publicado em | `docs/2026-03-13-cache-cnpj-doc.md` |
| Credenciais expostas | Nenhuma |
