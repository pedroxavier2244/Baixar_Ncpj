# API CNPJ — Release Notes v2.0

**Data:** Março 2026
**Base URL:** `http://5.189.163.33`
**Swagger:** `http://5.189.163.33/docs`

---

## Resumo das Melhorias

A versão 2.0 resolve um problema crítico de disco que afetou a atualização de março/2026 e entrega dois novos endpoints, cache mais longo e ciclo de atualização significativamente mais rápido.

| O que mudou | v1.0 | v2.0 |
|---|---|---|
| Endpoints disponíveis | 5 | **7** |
| Cache por CNPJ | 24 horas | **30 dias** |
| Rate limit `/cnpj/*` | 60 req/min | **2.000 req/min** |
| Rate limit `/search` | 30 req/min | **1.000 req/min** |
| Tempo total de atualização mensal | ~8–9 horas | **~3–4 horas** |
| Downtime durante atualização | ~2–3 horas | **0 minutos** |
| Falha de disco: requer intervenção manual | Sim | **Não — automático** |

---

## Novos Endpoints

### GET /cnpj/{cnpj}/participacoes

Dado um CNPJ, retorna todas as empresas em que ele figura como sócio. Útil para mapear grupos econômicos e holdings. Suporta paginação com `limit` e `offset`.

### GET /socios/buscar

Busca sócios pelo CPF parcial e/ou nome em toda a base (~70 M registros). Permite descobrir quantas empresas uma pessoa física administra. Parâmetros: `cpf_parcial`, `nome`, `limit`.

---

## Todos os Endpoints

| Endpoint | Auth | Cache | Descrição |
|---|---|---|---|
| `GET /health` | Não | — | Status da API, banco e cache |
| `GET /runs` | Não | — | Histórico de execuções do pipeline |
| `GET /cnpj/{cnpj}` | Sim | 30 dias | Dados completos da empresa |
| `GET /cnpj/{cnpj}/socios` | Sim | 30 dias | Quadro societário |
| `GET /cnpj/{cnpj}/participacoes` ★ | Sim | 30 dias | Empresas em que o CNPJ é sócio |
| `GET /search` | Sim | 5 min | Busca com filtros avançados |
| `GET /socios/buscar` ★ | Sim | 5 min | Busca sócio por CPF parcial ou nome |

★ Novos na v2.0

### Autenticação

Todos os endpoints marcados com **Sim** exigem o header `X-API-Key: <chave>`. Sem chave válida retorna `401`.

### Parâmetros de busca — /search

`razao_social`, `municipio`, `cnae`, `uf`, `situacao`, `porte`, `apenas_ativas`, `apenas_matriz`, `tem_telefone`, `order_by`, `order_dir`, `limit` (máx 100), `page`.

### Parâmetros de busca — /socios/buscar

`cpf_parcial`, `nome` (ao menos um obrigatório), `limit` (máx 100).

### Parâmetros — /cnpj/{cnpj}/participacoes

`limit` (máx 200, padrão 50), `offset` (paginação).

---

## Ciclo de Atualização Mensal

### Etapas e duração

| Etapa | Duração | Detalhe |
|---|---|---|
| Download | 30–60 min | ~20 GB de arquivos da Receita Federal |
| Extração e transformação | 30–60 min | Descompressão e normalização dos CSVs |
| Load + swap atômico | 60–90 min | Carrega staging, troca tabelas live/old em < 1 s |
| **Limpeza de disco** ★ | — | ZIPs e CSVs (~20 GB) deletados aqui automaticamente |
| REFRESH Materialized View | **20–40 min** | Antes levava 2–3 horas |
| Cleanup final | 5 min | Remove tabelas `_old`, registra status |
| **Total** | **~3–4 horas** | **Antes: ~8–9 horas** |

### Disponibilidade para os usuários

Durante todo o ciclo a API permanece disponível. O swap de tabelas é atômico (< 1 segundo). O `REFRESH MATERIALIZED VIEW` é executado de forma não bloqueante (`CONCURRENTLY`) — a API responde normalmente enquanto a view é reconstruída em background.

Se o banco ficar indisponível por qualquer motivo, o Redis serve respostas em cache por até **90 dias** (stale fallback). O campo `run_key` na resposta indica a competência do dado.

---

## Melhorias Técnicas

### Proteção automática contra disco cheio

O problema que causou falha manual em março/2026 foi eliminado com dois mecanismos:

**1. Limpeza pós-swap:** Ao fim do carregamento, antes de iniciar o REFRESH, o pipeline deleta automaticamente ZIPs e CSVs intermediários (~20 GB). Isso libera espaço precisamente no momento em que o PostgreSQL mais precisa de temp space.

**2. Fallback de disco:** Antes de cada REFRESH CONCURRENTLY, o sistema verifica o espaço livre. Se for menor que 50 GB, executa um DROP + full rebuild no lugar do REFRESH incremental — sem parar a API, sem intervenção humana. O threshold é configurável por variável de ambiente (`INDEX_MIN_FREE_GB`).

### PostgreSQL otimizado para o REFRESH

| Configuração | Antes | Depois |
|---|---|---|
| `work_mem` (sessão do REFRESH) | 4 MB (padrão) | **2 GB** |
| `max_parallel_workers_per_gather` | 2 (padrão) | **4** |
| Efeito | Spill para disco frequente | Sort em memória, sem spill |

O `work_mem = 2 GB` faz com que o PostgreSQL ordene os ~70 M registros da MV em memória RAM em vez de criar arquivos temporários no disco. Isso reduziu o tempo de REFRESH de **2–3 horas para 20–40 minutos**.

---

## Performance de Consulta

| Endpoint | 1ª consulta (banco) | Repetida (cache Redis) |
|---|---|---|
| `/cnpj/{cnpj}` | 80–150 ms | < 5 ms |
| `/cnpj/{cnpj}/socios` | 20–50 ms | < 5 ms |
| `/cnpj/{cnpj}/participacoes` | 20–80 ms | < 5 ms |
| `/search` | 200–800 ms | < 5 ms |
| `/socios/buscar` | 100–400 ms | < 5 ms |

---

## Dados e Convenções

**Datas:** string no formato `AAAAMMDD` — ex: `"20050416"` = 16/04/2005

**Capital social:** string com vírgula decimal — ex: `"61800000,00"` = R$ 61.800.000,00

**CNAE secundário:** códigos separados por vírgula — ex: `"3314710,4771702,5211799"`

**Matriz/Filial:** `"1"` = Matriz, `"2"` = Filial

**Situação cadastral:** `"01"` Nula · `"02"` Ativa · `"03"` Suspensa · `"04"` Inapta · `"08"` Baixada

**Porte:** `"00"` Não informado · `"01"` ME · `"03"` EPP · `"05"` Demais

**CPF dos sócios:** publicado pela Receita Federal parcialmente mascarado (`***NNNNNN**`). A API exibe o dado original.

**Competência dos dados:** consulte `last_success_run_key` no `/health`. Ex: `"2026-02"` = dados de fevereiro/2026.
A atualização ocorre tipicamente entre os dias 10–20 do mês seguinte à competência, quando a Receita Federal libera os arquivos.
