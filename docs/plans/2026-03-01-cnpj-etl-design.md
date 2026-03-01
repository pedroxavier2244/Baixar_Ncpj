# Design: ETL CNPJ Receita Federal

**Data:** 2026-03-01
**Projeto:** ETL CNPJ — Download, carga no PostgreSQL e API de consulta
**Ambiente:** VM Windows Server + PostgreSQL da empresa

---

## Objetivo

Baixar mensalmente os arquivos públicos de CNPJ da Receita Federal (via Nextcloud), processar e carregar no banco de dados PostgreSQL da empresa, e expor uma API REST para consulta por CNPJ, razão social, município e CNAE. O pipeline deve ser robusto, retomável e operar automaticamente via Task Scheduler.

---

## Fonte de dados

- **URL:** `https://arquivos.receitafederal.gov.br/index.php/s/YggdBLfdninEJX9`
- **Protocolo:** Nextcloud WebDAV (PROPFIND para listagem, GET para download)
- **Detecção de novo mês:** WebDAV PROPFIND compara `getlastmodified` dos arquivos com manifest local — sem baixar nada se não houver mudança
- **Arquivos relevantes:** Empresas, Estabelecimentos, Socios, Cnaes, Municipios, Naturezas, Qualificacoes, Motivos, Paises, Portes (ZIPs)

---

## Arquitetura

```
Task Scheduler (diário, 06h)
        │
        ▼
enqueue_job.py ──── WebDAV PROPFIND → compara com manifest local
        │           sem mudança → encerra
        │           mês novo   → cria job PENDING no SQLite
        ▼
worker.py ──────── pega job PENDING, adquire lease, inicia heartbeat
        │
        ▼
orchestrator.py ─── steps sequenciais com checkpoint
        │
        ├─▶ 1. download_step    → WebDAV download com retry + resume (.part)
        ├─▶ 2. verify_step      → SHA256 + tamanho + estrutura CSV
        ├─▶ 3. extract_step     → descompacta ZIPs → CSV por tipo
        ├─▶ 4. transform_step   → normaliza colunas/tipos em streaming
        ├─▶ 5. load_step        → COPY bulk no Postgres via psycopg3
        ├─▶ 6. index_step       → índices + REFRESH MATERIALIZED VIEW
        └─▶ 7. cleanup_step     → remove temporários, grava ultimo_status.json
```

---

## Estrutura de pastas

```
Documents/Baixar_Ncpj/
├── .env.example
├── .gitignore
├── requirements.txt
├── pipeline_control.db        # SQLite — gerado automaticamente
├── ultimo_status.json
│
├── config.py                  # Pydantic Settings
├── enqueue_job.py             # CLI: verifica novo mês, enfileira job
├── worker.py                  # loop: lease + heartbeat + execução
├── orchestrator.py            # executa steps com checkpoint
│
├── steps/
│   ├── __init__.py
│   ├── base.py                # classe base StepResult
│   ├── download_step.py
│   ├── verify_step.py
│   ├── extract_step.py
│   ├── transform_step.py
│   ├── load_step.py
│   ├── index_step.py
│   └── cleanup_step.py
│
├── db/
│   ├── control.py             # inicialização e queries do SQLite
│   ├── schema.sql             # schema PostgreSQL completo
│   └── control_schema.sql     # schema SQLite de controle
│
├── api/
│   ├── main.py                # FastAPI + Gunicorn lifespan + pool
│   ├── schemas.py             # Pydantic response models
│   └── routes/
│       ├── cnpj.py            # GET /cnpj/{cnpj}
│       ├── search.py          # GET /search
│       ├── health.py          # GET /health
│       └── runs.py            # GET /runs
│
├── logs/                      # etl_YYYY-MM-DD.log
├── checkpoints/               # <run_id>/manifest.json + artefatos
├── data/                      # ZIPs e CSVs temporários
│
└── scheduler/
    ├── install_tasks.ps1      # instala tarefas no Task Scheduler
    └── README_scheduler.md
```

---

## Banco de dados PostgreSQL

### Schema `cnpj` — tabelas fonte

| Tabela | Conteúdo |
|---|---|
| `rf_empresas` | Dados cadastrais da empresa (CNPJ base 8 dígitos) |
| `rf_estabelecimentos` | Filiais/matrizes com endereço, município, CNAE |
| `rf_socios` | Quadro societário |
| `rf_cnaes` | Descrições de CNAEs |
| `rf_municipios` | Nomes de municípios |
| `rf_naturezas` | Naturezas jurídicas |
| `rf_qualificacoes` | Qualificações de sócios |
| `rf_motivos` | Motivos de situação cadastral |
| `rf_paises` | Países |
| `rf_portes` | Portes de empresa |

### View materializada

```sql
cnpj.mv_cnpj_full  -- join empresas + estabelecimentos + auxiliares
                   -- cnpj_completo (14 dígitos) com índice GIN/btree
                   -- atualizada a cada carga (index_step)
```

### Índices essenciais

```sql
idx_mv_cnpj_completo        -- btree em cnpj_completo (lookup principal)
idx_mv_cnpj_base            -- btree em cnpj_base
idx_mv_razao_social         -- GIN trgm para busca textual
idx_mv_municipio            -- btree
idx_mv_cnae_fiscal          -- btree
idx_mv_uf                   -- btree
idx_mv_situacao_cadastral   -- btree
```

---

## API REST

| Método | Endpoint | Parâmetros | Descrição |
|---|---|---|---|
| GET | `/cnpj/{cnpj}` | cnpj: 8 ou 14 dígitos | Consulta completa |
| GET | `/search` | razao_social, municipio, cnae, uf, situacao, limit (max 100) | Busca filtrada |
| GET | `/health` | — | Status do serviço e último ETL |
| GET | `/runs` | limit | Histórico de execuções |

---

## Robustez

| Mecanismo | Proteção |
|---|---|
| Lease + heartbeat (30s) | Job travado → liberado automaticamente após expiração |
| Checkpoint por step | Retoma do step onde parou sem rebaixar tudo |
| Escrita atômica `.tmp` + `os.replace()` | Nunca arquivo corrompido |
| Retry com backoff exponencial (tenacity) | Download → 5 tentativas |
| Idempotência por `run_key` (YYYY-MM) | Não reprocessa mês já com SUCCESS |
| DLQ após `max_attempts` | Job → DEAD, gera relatório, não fica em loop |
| Logs estruturados JSON | Timestamp, step, run_id, level — fácil diagnóstico |

---

## Concorrência — 100 usuários simultâneos

- **Gunicorn** com 8 workers Uvicorn (async)
- **psycopg3** async com pool de 20 conexões (configurável)
- **mv_cnpj_full** pré-computada — queries de leitura rápidas sem JOIN em tempo real
- Índices GIN para busca textual (trigram `pg_trgm`)

---

## Configuração e execução

```bash
# 1. Setup
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 2. Configurar
cp .env.example .env
# editar POSTGRES_URL, DATA_DIR, LOG_DIR, etc.

# 3. Criar schema no Postgres
psql $POSTGRES_URL -f db/schema.sql

# 4. Executar manualmente (primeiro run)
python enqueue_job.py --run-key 2026-02
python worker.py --once

# 5. Subir API
gunicorn api.main:app -w 8 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000

# 6. Instalar Task Scheduler (PowerShell Admin)
.\scheduler\install_tasks.ps1
```

---

## Decisões técnicas

| Decisão | Escolha | Motivo |
|---|---|---|
| Detecção de novo mês | WebDAV PROPFIND + manifest local | Rápido, sem baixar nada desnecessário |
| Fila | SQLite job_queue | Sem dependências externas, suficiente para 1 worker |
| Carga Postgres | psycopg3 COPY + CSV temp | Mais rápido para milhões de linhas |
| API concorrência | Gunicorn + Uvicorn workers async | Simples de operar, sem orquestrador externo |
| Modelo de dados | Tabelas normalizadas + mv_cnpj_full | Performance de leitura + storage eficiente |
| Transform | Streaming em chunks (zipfile + csv) | Sem carregar tudo em RAM |
