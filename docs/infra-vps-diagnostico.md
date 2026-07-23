# Diagnóstico de Infraestrutura — VPS vmi3122361
**Data:** 2026-05-22 | **Servidor:** Contabo VPS (vmi3122361)

---

## Especificações do Servidor

| Recurso | Valor |
|---|---|
| **CPU** | 6 vCPUs — AMD EPYC @ 2.0 GHz |
| **RAM** | 11.68 GB |
| **Disco** | 193 GB SSD |
| **OS** | Ubuntu Linux |
| **Gerenciador** | Coolify + Docker |

---

## Status Geral — Alertas Críticos

| # | Alerta | Severidade |
|---|---|---|
| 1 | **Swap 100% cheio** (4 GB / 4 GB) — sistema em pressão de memória | CRÍTICO |
| 2 | **Projeto Discador usando 225% de CPU** (2.25 de 6 cores) | CRÍTICO |
| 3 | **Disco em 89%** — cresce ~10–15 GB por mês (CNPJ mensal) | ALTO |
| 4 | **ETL-HML** com 32% CPU idle — ambiente de homologação não deveria consumir tanto | MÉDIO |
| 5 | **CNPJ Redis cache hit rate: 6%** — redis recém reiniciado ou baixo tráfego | BAIXO |

---

## Recursos Totais

```
RAM:    7.7 GB usada + 4.0 GB swap (100%) = 11.7 GB total comprometido
        Disponível real: 4.0 GB (em buffer/cache, reclaimável)
DISCO:  171 GB usados / 193 GB (89%) — 22 GB livres
CPU:    ~50% médio no snapshot (300% de 600% possíveis)
```

> O swap totalmente cheio significa que em algum momento o sistema ficou sem RAM e começou a usar disco como memória. Isso degrada performance significativamente — qualquer pico de uso (ex: pipeline ETL rodando) pode causar lentidão ou OOM.

---

## Inventário por Projeto

### 1. CNPJ API *(projeto principal)*

> Pipeline ETL mensal que baixa ~70 milhões de registros da Receita Federal, transforma e carrega no PostgreSQL. Serve esses dados via API REST (FastAPI) com cache Redis. Usado para consultar dados de empresas, sócios e estabelecimentos por CNPJ.

| Container | RAM | CPU |
|---|---|---|
| `cnpj_api` | 119.5 MB | 7.3% |
| `cnpj_worker` | 12 MB | 0% (idle) |
| `cnpj_nginx` | 5.4 MB | 0.1% |
| `cnpj_redis` | 180.9 MB | 2.5% |
| **Total** | **~318 MB** | **~10%** |

**Disco:**
| Volume | Tamanho |
|---|---|
| `implementation_postgres_data` | 96 GB *(compartilhado com CRM)* |
| `cnpj_etl_logs` | 12 MB |
| `cnpj_etl_checkpoints` | 248 KB |

**Porta externa:** 80 (nginx)
**Crescimento mensal:** +10–15 GB (PostgreSQL) + ~20 GB temporários durante o ETL (limpos após carga)

---

### 2. CRM / Implementation

> CRM interno com pipeline ETL de dados. Gerencia relacionamento com clientes, processa e armazena dados via workers ETL, usa MinIO para armazenamento de arquivos e tem monitoramento via Prometheus/Grafana. O banco `etl_db` é exclusivo deste sistema — não confundir com o `cnpj_db`.

| Container | RAM | CPU |
|---|---|---|
| `implementation-postgres` | **1.19 GB** | 6.2% |
| `implementation-api` | 30.2 MB | 10.8% |
| `implementation-minio` | 103.8 MB | 0.04% |
| `implementation-worker-etl` | 7.5 MB | 0.8% |
| `implementation-worker-beat` | 9.5 MB | 0% |
| `implementation-redis` | 1.1 MB | 2.3% |
| **Total** | **~1.35 GB** | **~20%** |

**Disco:**
| Volume | Tamanho |
|---|---|
| `implementation_postgres_data` | 96 GB *(compartilhado com CNPJ)* |
| `implementation_minio_data` | 1.1 GB |
| `implementation_prometheus_data` | 156 MB |
| `implementation_grafana_data` | 1.1 MB |

**Porta externa:** 8000

> O PostgreSQL de 96 GB hospeda **dois bancos**: `cnpj_db` (CNPJ com 70M linhas) e `etl_db` (CRM). Cresce conforme novas cargas mensais do CNPJ.

---

### 3. ETL Homologação

> Ambiente de **staging/teste** do pipeline ETL do CRM (Implementation). Usado para validar mudanças antes de aplicar em produção. Não deve rodar continuamente — só ligar quando estiver testando algo. O alto consumo de CPU em idle é anormal e indica configuração de health check agressiva ou jobs fantasma.

| Container | RAM | CPU |
|---|---|---|
| `etl-hml-postgres` | 16 MB | **12.5%** |
| `etl-hml-minio` | 96.4 MB | 2.8% |
| `etl-hml-redis` | 3.8 MB | **12%** |
| `etl-hml-api` | 16.4 MB | 4.3% |
| `etl-hml-worker-etl` | 7 MB | 0.6% |
| **Total** | **~140 MB** | **~32%** |

**Disco:**
| Volume | Tamanho |
|---|---|
| `etl-hml_postgres_hml_data` | 47 MB |
| `etl-hml_minio_hml_data` | 42 MB |

**Portas externas:** 8100 (api), 9100 (minio)

> Ambiente de homologação consumindo 32% de CPU é anormal. Postgres e Redis estão em polling alto. Investigar se há jobs rodando ou configuração de health check agressiva.

---

### 4. Coolify *(painel de gerenciamento)*

> Plataforma self-hosted de deploy e gerenciamento de containers (similar ao Heroku/Railway). Gerencia os deploys de todos os projetos da VPS via interface web na porta 8090. Permite criar apps, configurar variáveis de ambiente, ver logs e reiniciar serviços sem precisar de SSH.

| Container | RAM | CPU |
|---|---|---|
| `coolify` | 300.9 MB | 6.1% |
| `coolify-realtime` | 41.6 MB | 9.8% |
| `coolify-redis` | 7.7 MB | 10.1% |
| `coolify-sentinel` | 20.9 MB | 0% |
| `coolify-helper` | 560 KB | 0.01% |
| **Total** | **~372 MB** | **~26%** |

**Disco:**
| Volume | Tamanho |
|---|---|
| `coolify-db` | 107 MB |

**Porta externa:** 8090

> Coolify consome 26% de CPU constantemente — overhead significativo de gerenciamento. Se o time não usa o painel ativamente, esse custo é puro overhead.

---

### 5. Projeto Discador *(VoIP / Asterisk)*

> Sistema de discagem automática VoIP. Usa Asterisk como central telefônica (PBX) para realizar e receber chamadas via SIP/RTP. A interface gerencia os ramais, filas e campanhas de discagem. **Atualmente causando sobrecarga crítica de CPU** (225%) — investigar chamadas travadas ou loops de processamento.

| Container | RAM | CPU |
|---|---|---|
| `projeto-discador-interface` | 26.8 MB | **61.3%** |
| `projeto-discador-asterisk` | 51.7 MB | **164%** |
| **Total** | **~79 MB** | **⚠️ 225%** |

**Portas externas:** 5000 (interface), 5038 (AMI), 5060 (SIP), 5070 (SIP alt), 8088 (WebSocket), 10000–10100 (RTP/audio)

> **PROBLEMA CRÍTICO**: O Asterisk sozinho está consumindo mais de 2 CPUs (164%). Isso é anormal para um sistema VoIP — indica ligações ativas com transcodificação de áudio, loop de processamento com bug, ou configuração incorreta. Está degradando todos os outros serviços na VPS.

---

### 6. Bot Builder

> Plataforma para criar e gerenciar chatbots de WhatsApp. Permite montar fluxos de atendimento automatizado, configurar respostas e integrar com outros sistemas. Tem banco de dados próprio (`bot_builder` no `botbuilder-postgres`, porta 5433).

| Container | RAM | CPU |
|---|---|---|
| `nd56ffa40uvrgq0kdovg9q9h` | 74.1 MB | 0% |
| `botbuilder-postgres` | 30.3 MB | 0.01% |
| **Total** | **~104 MB** | **~0%** |

**Porta externa:** 6767

---

### 7. Organograma

> Aplicação web para visualização da estrutura hierárquica de equipes ou empresas. Exibe cargos, responsáveis e relações entre departamentos em formato de organograma interativo.

| Container | RAM | CPU |
|---|---|---|
| `organograma_app` | 56.4 MB | 0% |
| **Total** | **~56 MB** | **0%** |

**Porta externa:** 3001

---

### 8. Integration API

> *(descrição pendente — me diga o que este serviço faz para atualizar)*

| Container | RAM | CPU |
|---|---|---|
| `integration-api` | 59.5 MB | 5.3% |
| **Total** | **~60 MB** | **~5%** |

**Porta externa:** 8002

---

### 9. Obsidian CouchDB *(sync pessoal)*

> Banco de dados CouchDB rodando o plugin **Self-hosted LiveSync** do Obsidian. Sincroniza as notas do Obsidian entre dispositivos (celular, notebook, desktop) sem depender de serviços de nuvem externos. Uso pessoal — não está ligado a nenhum produto.

| Container | RAM | CPU |
|---|---|---|
| `obsidian-couchdb` | 22.7 MB | 7.9% |
| **Total** | **~23 MB** | **~8%** |

**Porta externa:** 5984

---

### 10. Projetos via Coolify (não identificados)
| Container | RAM | CPU | Porta |
|---|---|---|---|
| `axgcttjkws5k1hkcgp5wc8cs` | 5.5 MB | 0% | 8080 |
| `cxsi7zaixnk61yp8r412gr6d` | 22.3 MB | 0.07% | 5001 |

---

## Resumo Consolidado

| Projeto | RAM | CPU | Disco |
|---|---|---|---|
| CNPJ API | 318 MB | ~10% | 96 GB (compartilhado) |
| CRM / Implementation | 1.35 GB | ~20% | 96 GB (compartilhado) + 1.3 GB |
| ETL Homologação | 140 MB | ~32% | ~89 MB |
| **Coolify** | **372 MB** | **~26%** | 107 MB |
| **Discador Asterisk** | **79 MB** | **⚠️ 225%** | — |
| Bot Builder | 104 MB | ~0% | — |
| Organograma | 56 MB | 0% | — |
| Integration API | 60 MB | ~5% | — |
| Obsidian CouchDB | 23 MB | ~8% | — |
| Outros (Coolify-deployed) | 28 MB | ~0% | — |
| **TOTAL IDENTIFICADO** | **~2.5 GB** | **~326%** | **~98 GB** |
| SO + Docker daemon + cache | ~5.2 GB | — | ~73 GB |
| **TOTAL VPS** | **7.7 GB + 4 GB swap** | **~50% média** | **171 GB / 193 GB** |

---

## Projeção de Crescimento de Disco

| Mês | Disco Estimado | % |
|---|---|---|
| Hoje (mai/2026) | 171 GB | 89% |
| Jun/2026 | ~184 GB | 95% |
| Jul/2026 | ~197 GB | **> 100%** ← disco cheio |

> O disco vai encher em ~2 meses sem intervenção. O PostgreSQL cresce ~10–15 GB por carga mensal do CNPJ.

---

## Recomendações

### Ação Imediata
1. **Investigar Asterisk (Discador)** — 225% CPU é anormal. Verificar se há chamadas travadas, loops ou transcodificação desnecessária. Pode ser a causa principal do swap cheio.
2. **Ampliar disco** — mínimo +100 GB no Contabo (bloco adicional) antes da próxima carga CNPJ.

### Curto Prazo (1–2 meses)
3. **Separar o ETL-HML ou desligar** — 32% CPU idle em ambiente de homologação é desperdício. Só ligar quando for usar.
4. **Upgrade de RAM** — 11 GB com swap 100% cheio indica que o servidor precisa de 16 GB mínimo para rodar o pipeline ETL sem pressão de memória.


---

*Gerado em 2026-05-22 com base em `docker stats`, `df`, `du` e `docker system df`.*
