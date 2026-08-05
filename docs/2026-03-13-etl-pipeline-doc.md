# Documentação do Pipeline ETL — CNPJ Receita Federal

**Data:** 2026-03-13
**Revisão:** Time de Infraestrutura
**Status:** Publicado

---

## O QUE É?

Pipeline ETL responsável pelo processamento mensal dos dados públicos de empresas da Receita Federal do Brasil. Baixa, valida, transforma e carrega ~70 milhões de registros no banco de dados de produção, mantendo a API CNPJ sempre atualizada com a competência mais recente.

---

## Visão Geral

```
RF WebDAV → [download] → [verify] → [extract] → [transform] → [load] → [index] → API
```

| Etapa | Step | Responsável | Artefato gerado |
|---|---|---|---|
| 1 | download | `steps/download_step.py` | `download_manifest.json` |
| 2 | verify | `steps/verify_step.py` | `verify_manifest.json` |
| 3 | extract | `steps/extract_step.py` | `extract_manifest.json` |
| 4 | transform | `steps/transform_step.py` | `transform_manifest.json` |
| 5 | load | `steps/load_step.py` | `load_manifest.json` |
| 6 | index | `steps/index_step.py` | `index_manifest.json` |
| 7 | cleanup | interno | `ultimo_status.json` |

---

## Ferramenta de Orquestração e Infraestrutura

| Item | Valor |
|---|---|
| Orquestrador | `orchestrator.py` — Python puro, sem Airflow/Prefect |
| Controle de jobs | SQLite (`pipeline_control.db`) com fila e heartbeat |
| Worker | `worker.py` — processo único, polling a cada 60s |
| Provedor de nuvem | VPS própria (Contabo) — `5.189.163.33` |
| Serviço de computação | Docker container `cnpj-worker` |
| Banco de destino | PostgreSQL 16 (Alpine) — Docker container |
| Cache | Redis 7 (Alpine) — Docker container |
| SO | Debian/Ubuntu no host |

---

## Frequência e Janela de Processamento

| Item | Valor |
|---|---|
| Frequência | Mensal — dados publicados pela RF entre D+1 e D+5 |
| Prazo de carga | D+15 após fechamento da competência |
| Duração real (última execução) | ~2h 40min (2026-03-12: 18:43 → 21:27) |
| Janela crítica | `index_step` — REFRESH CONCURRENTLY da MV (~2h30) |
| Execuções simultâneas | **1** — fila serializada por lease/heartbeat |
| Paralelismo interno | Nenhum — steps sequenciais, sem threads por tabela |

---

## Volume por Execução

| Tabela | Linhas (fev/2026) |
|---|---|
| rf_empresas | ~55 milhões |
| rf_estabelecimentos | ~60 milhões |
| rf_socios | ~27 milhões |
| rf_simples | ~47 milhões |
| rf_cnaes | 1.359 |
| rf_municipios | 5.572 |
| rf_naturezas | 91 |
| rf_qualificacoes | 68 |
| rf_motivos | 63 |
| rf_paises | 254 |
| **Total carregado** | **~210 milhões de linhas** |
| **mv_cnpj_full (MV)** | **~69,8 milhões de linhas** |

---

## Mapeamento de Campos

### rf_empresas

| Campo origem (RF) | Tipo origem | Transformação | Campo destino | Tipo destino |
|---|---|---|---|---|
| col 1 | latin-1 text | strip | `cnpj_basico` | CHAR(8) PK |
| col 2 | latin-1 text | strip | `razao_social` | TEXT |
| col 3 | text | strip | `natureza_juridica` | CHAR(4) |
| col 4 | text | strip | `qualificacao_responsavel` | CHAR(2) |
| col 5 | text | strip, `00000000`→NULL | `capital_social` | TEXT |
| col 6 | text | strip | `porte` | CHAR(2) |
| col 7 | text | strip | `ente_federativo_responsavel` | TEXT |
| — | — | gerado no load | `run_key` | CHAR(7) |
| — | — | DEFAULT NOW() | `created_at` | TIMESTAMPTZ |
| — | — | DEFAULT NOW() | `updated_at` | TIMESTAMPTZ |

### rf_estabelecimentos

| Campo origem (RF) | Transformação | Campo destino | Tipo destino |
|---|---|---|---|
| col 1 | strip | `cnpj_basico` | CHAR(8) PK |
| col 2 | strip | `cnpj_ordem` | CHAR(4) PK |
| col 3 | strip | `cnpj_dv` | CHAR(2) PK |
| col 4 | strip | `identificador_matriz_filial` | CHAR(1) |
| col 5 | strip | `nome_fantasia` | TEXT |
| col 6 | strip | `situacao_cadastral` | CHAR(2) |
| col 7 | strip, `00000000`→NULL | `data_situacao_cadastral` | CHAR(8) |
| col 8 | strip | `motivo_situacao_cadastral` | CHAR(2) |
| col 9–10 | strip | `nm_cidade_exterior`, `pais` | TEXT, CHAR(3) |
| col 11 | strip, `00000000`→NULL | `data_inicio_atividade` | CHAR(8) |
| col 12 | strip | `cnae_fiscal` | CHAR(7) |
| col 13 | strip | `cnae_fiscal_secundaria` | TEXT |
| col 14–21 | strip | endereço completo | TEXT/CHAR |
| col 22–27 | strip | telefones e fax | TEXT |
| col 28 | strip, lower | `correio_eletronico` | TEXT |
| col 29–30 | strip | situação especial | TEXT/CHAR(8) |

### rf_socios

| Campo origem (RF) | Transformação | Campo destino | Tipo destino |
|---|---|---|---|
| col 1 | strip | `cnpj_basico` | CHAR(8) |
| col 2 | strip | `identificador_socio` | CHAR(1) |
| col 3 | strip | `nome_socio` | TEXT |
| col 4 | strip | `cnpj_cpf_socio` | TEXT |
| col 5 | strip | `qualificacao_socio` | CHAR(2) |
| col 6 | strip, `00000000`→NULL | `data_entrada_sociedade` | CHAR(8) |
| col 7 | strip | `pais` | CHAR(3) |
| col 8 | strip | `representante_legal` | TEXT |
| col 9 | strip | `nome_representante` | TEXT |
| col 10 | strip | `qualificacao_representante` | CHAR(2) |
| col 11 | strip | `faixa_etaria` | CHAR(1) |

**Faixas etárias RF:**

| Código | Faixa |
|---|---|
| 1 | 0–12 anos |
| 2 | 13–20 anos |
| 3 | 21–30 anos |
| 4 | 31–40 anos |
| 5 | 41–50 anos |
| 6 | 51–60 anos |
| 7 | 61–70 anos |
| 8 | 71–80 anos |
| 9 | 81–150 anos |
| 0 | Não informado |

### rf_simples

| Campo origem (RF) | Transformação | Campo destino | Tipo destino |
|---|---|---|---|
| col 1 | strip | `cnpj_basico` | CHAR(8) PK |
| col 2 | strip | `opcao_pelo_simples` | CHAR(1) |
| col 3 | strip, `00000000`→NULL | `data_opcao_simples` | CHAR(8) |
| col 4 | strip, `00000000`→NULL | `data_exclusao_simples` | CHAR(8) |
| col 5 | strip | `opcao_pelo_mei` | CHAR(1) |
| col 6 | strip, `00000000`→NULL | `data_opcao_mei` | CHAR(8) |
| col 7 | strip, `00000000`→NULL | `data_exclusao_mei` | CHAR(8) |

### Tabelas de Lookup (cnaes, municipios, naturezas, qualificacoes, motivos, paises, portes)

| Campo origem | Transformação | Campo destino | Tipo destino |
|---|---|---|---|
| col 1 | strip | `codigo` | CHAR(2–7) PK |
| col 2 | strip | `descricao` | TEXT |

---

## Staging Area

| Item | Valor |
|---|---|
| Schema | `cnpj_staging` |
| Tipo | UNLOGGED tables (sem WAL = máxima velocidade de COPY) |
| Estratégia | Tabelas `_new` criadas ao lado das tabelas live; swap atômico ao final |
| Sem constraints durante COPY | Indexes criados somente pós-COPY |
| Retenção | Tabelas `_old` removidas pelo `index_step` após refresh da MV |

---

## Banco de Destino

| Item | Valor |
|---|---|
| Tipo | PostgreSQL 16 |
| Provedor | VPS self-hosted (Docker container) |
| Região | Europa (servidor Contabo) |
| Schema principal | `cnpj` |
| Schema serving | `cnpj_serving` |
| Estratégia de carga | Atomic swap (`_new` → rename → `_old` descartada) |
| Extensão | `pg_trgm` (busca fuzzy por trigrama) |
| MV principal | `cnpj_serving.mv_cnpj_full` — join denormalizado de 7 tabelas |

---

## Política de Idempotência

Cada step persiste seu status em SQLite (`pipeline_control.db`):

- **SUCCESS** → step é ignorado em re-execução
- **FAILED** → step é re-executado do zero
- **Artefatos atômicos** → escritos como `.tmp` e renomeados ao sucesso
- **Tabelas `_new`** → descartadas no início do step se existirem de run anterior falha
- **Sem UPDATE/DELETE** em tabelas live → zero bloat de MVCC

Em caso de falha em qualquer step, o pipeline para imediatamente. As tabelas live permanecem intactas (swap não foi executado). A re-execução retoma a partir do step que falhou.

---

## Monitoramento e Alertas

| Item | Mecanismo |
|---|---|
| Status por step | `pipeline_control.db` (SQLite) — campo `status`, `error_msg`, `finished_at` |
| Status da última execução | `ultimo_status.json` |
| Health da API | `GET /health` — retorna `last_success_run_key`, `mv_row_count`, `db_ok`, `cache_ok` |
| Heartbeat do worker | Thread dedicada renova lease a cada 30s — job expira se worker morrer |
| Logs | stdout/stderr do container Docker (`docker compose logs worker`) |
| Alertas ativos | Nenhum automatizado — monitoramento manual via `/health` |

---

## Revisão Técnica

| Item | Status |
|---|---|
| Revisado por | Time de Infraestrutura |
| Data da revisão | 2026-03-13 |
| Publicado em | `docs/2026-03-13-etl-pipeline-doc.md` no repositório `Baixar_Ncpj` |
| Credenciais expostas | Nenhuma |
