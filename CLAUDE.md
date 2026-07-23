# CLAUDE.md — BANCO CNPJ

Instruções específicas deste projeto. Seguem as regras do CLAUDE.md global e as complementam.

---

## O que é este sistema

Pipeline ETL + API REST para dados da Receita Federal (CNPJ público).
Baixa ~70 milhões de registros mensalmente, transforma, carrega no PostgreSQL e serve via FastAPI com cache Redis.

**Repositório local:** `C:\Users\MB NEGOCIOS\Desktop\BANCO CNPJ`
**Branch principal:** `master` | **Branch de desenvolvimento:** `nova`

---

## Infraestrutura de produção (VPS)

- **VPS:** Ubuntu Linux (`vmi3122361`)
- **PostgreSQL:** container `54bc71b1fc31_implementation-postgres-1`
  - usuário: `etl_user`
  - banco CNPJ: `cnpj_db`
  - banco ETL/CRM (outro sistema): `etl_db` ← não confundir
- **Containers CNPJ:** `cnpj_api`, `cnpj_worker`, `cnpj_nginx`, `cnpj_redis`
- **Redis:** container `88ab8fd17ca0_cnpj_redis`
- **API:** acessível em `127.0.0.1:8001` (nginx fronta na porta 80)

### Comando para acessar o banco CNPJ na VPS

```bash
docker exec -it 54bc71b1fc31_implementation-postgres-1 psql -U etl_user -d cnpj_db
```

### Schemas PostgreSQL (dentro do cnpj_db)

| Schema | Uso |
|---|---|
| `cnpj` | Dados vivos — tabelas principais |
| `cnpj_staging` | Tabelas UNLOGGED temporárias (ETL) |
| `cnpj_serving` | Materialized views para a API |

**Nunca usar o schema `public`.**

---

## Tabelas principais (schema `cnpj`)

| Tabela | Descrição |
|---|---|
| `rf_empresas` | Dados da empresa (razão social, natureza jurídica, porte) — PK: `cnpj_basico CHAR(8)` |
| `rf_estabelecimentos` | Endereço, CNAE, situação cadastral, data de abertura — PK: `(cnpj_basico, cnpj_ordem, cnpj_dv)` |
| `rf_socios` | Sócios e representantes |
| `rf_simples` | Simples Nacional / MEI — `opcao_pelo_mei CHAR(1)`, `opcao_pelo_simples CHAR(1)` |
| `rf_cnaes` | Lookup: código CNAE → descrição |
| `rf_municipios` | Lookup: código → nome do município |
| `rf_naturezas` | Lookup: código → natureza jurídica |
| `rf_qualificacoes` | Lookup: código → qualificação do sócio |
| `rf_motivos` | Lookup: código → motivo da situação cadastral |
| `rf_paises` | Lookup: código → país |

### Materialized view principal

`cnpj_serving.mv_cnpj_full` — JOIN completo de empresas + estabelecimentos + todos os lookups + simples/MEI.
Usada pela API. Após carga, executar:
```sql
REFRESH MATERIALIZED VIEW CONCURRENTLY cnpj_serving.mv_cnpj_full;
```

---

## Convenções de dados

- `data_inicio_atividade` → `CHAR(8)` formato `YYYYMMDD` (ex: `20260115`)
- `cnpj_basico` → `CHAR(8)` sem formatação
- `cnpj_ordem` → `CHAR(4)` — matrizes sempre `'0001'`
- `opcao_pelo_mei` / `opcao_pelo_simples` → `'S'` = sim, `'N'` = não, `NULL` = sem info
- `situacao_cadastral` → `'02'` = ativa, `'08'` = baixada, `'03'` = suspensa, `'04'` = inapta
- Capital social: texto com formato brasileiro (`1.234,56`) — não parsear como número direto

### Filtrar apenas matrizes

Sempre usar `cnpj_ordem = '0001'` para contar empresas únicas.
Sem esse filtro, filiais duplicam o resultado.

---

## Queries úteis de referência

### Empresas abertas em determinado ano, por classificação
```sql
SELECT
    CASE
        WHEN s.opcao_pelo_mei = 'S' THEN 'MEI'
        WHEN s.opcao_pelo_simples = 'S' AND COALESCE(s.opcao_pelo_mei,'N') != 'S' THEN 'Simples Nacional (não MEI)'
        ELSE 'Outros'
    END AS classificacao,
    COUNT(*) AS total
FROM cnpj.rf_estabelecimentos est
LEFT JOIN cnpj.rf_simples s ON s.cnpj_basico = est.cnpj_basico
WHERE est.data_inicio_atividade LIKE '2026%'
  AND est.cnpj_ordem = '0001'
GROUP BY classificacao
ORDER BY total DESC;
```

### Verificar última carga
```sql
SELECT DISTINCT run_key FROM cnpj.rf_empresas ORDER BY run_key DESC LIMIT 3;
```

### Contar empresas ativas
```sql
SELECT COUNT(*) FROM cnpj.rf_estabelecimentos
WHERE situacao_cadastral = '02' AND cnpj_ordem = '0001';
```

---

## Arquivos do projeto

```
BANCO CNPJ/
├── Baixar_Ncpj/            ← Codebase principal de produção
│   ├── api/                ← FastAPI (routes/, main.py, schemas.py)
│   ├── db/
│   │   ├── schema.sql      ← Schema PostgreSQL (idempotente)
│   │   ├── control.py      ← Fila de jobs SQLite
│   │   └── migrations/     ← Scripts de migração numerados
│   ├── steps/              ← 7 etapas do pipeline ETL
│   ├── tests/              ← pytest (11 módulos)
│   ├── config.py           ← Pydantic Settings (todas as variáveis)
│   ├── orchestrator.py     ← Motor do pipeline (chama os 7 steps)
│   ├── worker.py           ← Loop do job (produção)
│   ├── enqueue_job.py      ← Detecta nova versão RF → cria job
│   ├── docker-compose.yml  ← Stack de produção
│   └── .env / .env.example
├── db/schema.sql           ← Versão raiz (referência)
└── config.py               ← Versão raiz (referência)
```

**Atenção:** Existem arquivos duplicados na raiz e em `Baixar_Ncpj/`. O codebase **ativo em produção** é sempre `Baixar_Ncpj/`. Antes de editar qualquer arquivo, confirmar qual versão é a correta.

---

## Pipeline ETL — 7 etapas

| # | Step | O que faz | Duração |
|---|---|---|---|
| 1 | `download_step` | Baixa ZIPs do WebDAV da RF (com resume) | 30–60 min |
| 2 | `verify_step` | Valida checksums | ~5 min |
| 3 | `extract_step` | Descompacta CSVs | 30–60 min |
| 4 | `transform_step` | Normaliza, mascara CPF, codificação UTF-8 | 30–60 min |
| 5 | `load_step` | COPY → staging → **atomic schema swap** (< 1s downtime) | 60–90 min |
| 6 | `cleanup_step` | Deleta ZIPs + CSVs (~20 GB) | rápido |
| 7 | `index_step` | `REFRESH MATERIALIZED VIEW CONCURRENTLY` | 20–40 min |

O pipeline é **checkpointed**: se falhar em algum step, retoma do ponto de falha.

---

## Deploy na VPS

```bash
# Local (Windows — usar ; não &&)
git add .; git commit -m "mensagem"; git push

# VPS (bash)
cd /opt/cnpj  # ou onde estiver o projeto
git pull
docker compose down && docker compose up -d --build
docker ps  # verificar se cnpj_api está healthy
```

**Verificar saúde da API:**
```bash
curl http://localhost:8001/health
```

---

## Variáveis de ambiente obrigatórias

Ver `.env.example` em `Baixar_Ncpj/`. Principais:

| Variável | Descrição |
|---|---|
| `POSTGRES_URL` | `postgresql://etl_user:SENHA@host:5432/cnpj_db` |
| `REDIS_URL` | `redis://cnpj_redis:6379/0` |
| `API_KEY` | Chave `X-API-Key` para autenticar na API |
| `WEBDAV_TOKEN` | Token para baixar dados da RF |
| `DATA_DIR` | Diretório para ZIPs e CSVs temporários |

---

## Diagnóstico rápido — problemas comuns

### `db_ok: false` no `/health`

O health check (`api/routes/health.py`) só marca `db_ok = true` se:
1. O pool de conexões conseguir abrir uma conexão com o PostgreSQL, **E**
2. A query em `pg_matviews` retornar uma linha com `ispopulated = true` para `cnpj_serving.mv_cnpj_full`

Todas as exceções são silenciadas com `pass` — o endpoint nunca mostra o erro real.

**Causas possíveis (em ordem de probabilidade):**

| Causa | Sintoma adicional |
|---|---|
| Container PostgreSQL caído | `docker ps` não mostra o container |
| `mv_cnpj_full` não populada (`ispopulated = false`) | View existe mas REFRESH falhou ou está em andamento |
| Pool sem conexões disponíveis | Logs da API mostram `pool exhausted` ou timeout |
| Schema/view removida acidentalmente | Query retorna 0 linhas |

**Sequência de diagnóstico (rodar na VPS):**

```bash
# 1. Container está de pé?
docker ps | grep postgres

# 2. Rodar a query exata do health check
docker exec -it 54bc71b1fc31_implementation-postgres-1 psql -U etl_user -d cnpj_db -c "
SELECT mv.ispopulated, c.reltuples::bigint AS approx_rows
FROM pg_matviews mv
JOIN pg_class c ON c.relname = mv.matviewname
WHERE mv.schemaname = 'cnpj_serving'
  AND mv.matviewname = 'mv_cnpj_full';
"

# 3. Ver o erro real nos logs da API
docker logs cnpj_api --tail 50 2>&1 | grep -i "error\|exception\|pool\|connect"
```

**Correções conforme causa:**

- Container caído → `docker compose up -d postgres`
- `ispopulated = false` → `REFRESH MATERIALIZED VIEW CONCURRENTLY cnpj_serving.mv_cnpj_full;`
- View não existe → recriar via `db/schema.sql` (seção `cnpj_serving`)
- **DNS failure (`failed to resolve host 'implementation-postgres-1'`)** → o container postgres perdeu a rede (ver seção abaixo)

---

### Container postgres perde a rede (`failed to resolve host`)

**Contexto:** O `implementation-postgres-1` é gerenciado pelo stack `implementation` (docker-compose separado). Se a rede `implementation_default` for recriada, o container postgres fica orphan — continua rodando mas sem rede. O `cnpj_api` não consegue mais resolver o hostname.

**Diagnóstico:**
```bash
# Ver se o postgres tem redes
/usr/bin/docker inspect 54bc71b1fc31_implementation-postgres-1 --format '{{json .NetworkSettings.Networks}}'
# Se retornar {} → perdeu a rede
```

**Fix (2026-04 — incidente real):**
```bash
# 1. Limpar rota obsoleta no namespace do container
PID=$(/usr/bin/docker inspect --format '{{.State.Pid}}' 54bc71b1fc31_implementation-postgres-1)
nsenter -t $PID -n ip route del 172.18.0.0/16

# 2. Reconectar à rede com alias correto
/usr/bin/docker network connect --alias implementation-postgres-1 implementation_default 54bc71b1fc31_implementation-postgres-1

# 3. Confirmar
/usr/bin/docker exec cnpj_api getent hosts implementation-postgres-1
curl http://localhost:8001/health
```

**Atenção:** O `implementation-postgres-1` tem `127.0.0.1:5432` no HostConfig. O `botbuilder-postgres` também usava essa porta — por isso foi movido para `127.0.0.1:5433` durante o incidente de abril/2026.

**Portas atuais dos bancos na VPS:**

| Container | Porta host | Banco |
|---|---|---|
| `54bc71b1fc31_implementation-postgres-1` | `127.0.0.1:5432` | `cnpj_db` (CNPJ API) |
| `botbuilder-postgres` | `127.0.0.1:5433` | `bot_builder` (WhatsApp bot) |
| `etl-hml-postgres-1` | sem porta host | `etl_db` (homologação) |

---

### `hit_rate` baixo no `/health`

Cache hit rate abaixo de ~30% é sinal de que o Redis está respondendo mas as consultas não estão sendo servidas do cache (usuários novos, chaves expirando, ou a API acabou de subir). Não é problema isolado — só preocupar se `hit_rate < 0.05` por mais de 24h com tráfego normal.

---

### Verificar status geral da stack

```bash
# Todos os containers rodando?
docker ps --format "table {{.Names}}\t{{.Status}}" | grep cnpj

# Saúde da API
curl http://localhost:8001/health | python3 -m json.tool

# Logs recentes de erro (todos os containers)
docker compose logs --tail 30 2>&1 | grep -i error
```

---

## Regras específicas deste projeto

- **Nunca rodar DROP/DELETE/TRUNCATE** nas tabelas `cnpj.*` sem confirmação explícita — são dados de produção (~70M linhas).
- **Nunca re-executar steps já concluídos** — verificar estado do job em `pipeline_control.db` antes.
- **Sempre usar `cnpj_ordem = '0001'`** ao contar empresas para evitar dupla contagem de filiais.
- **`data_inicio_atividade` não tem index** — queries com LIKE '2026%' fazem full scan (~40M linhas). Podem demorar 1–5 min. Adicionar index se virar consulta frequente.
- **Não confundir `etl_db` com `cnpj_db`** — são bancos distintos no mesmo container PostgreSQL. `etl_db` é de outro sistema (CRM/ETL interno).
- **Encoding UTF-8 obrigatório** em todos os arquivos do pipeline — o histórico tem casos de mojibake (UTF-8 lido como latin-1).
