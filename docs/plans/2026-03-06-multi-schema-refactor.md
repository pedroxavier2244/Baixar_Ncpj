# Multi-Schema Refactor + Simples Nacional Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reorganizar o projeto para usar três schemas PostgreSQL isolados (`cnpj_staging`, `cnpj`, `cnpj_serving`), adequado para banco corporativo compartilhado, e adicionar suporte completo ao arquivo `Simples.zip` da Receita Federal (Simples Nacional / MEI).

**Architecture:** Staging tables vivem em `cnpj_staging` (UNLOGGED, sem constraints), dados finais em `cnpj` (tabelas tratadas com PKs e índices), e a materialized view de consulta em `cnpj_serving`. O código Python usa `settings.pg_schema`, `settings.pg_staging_schema` e `settings.pg_serving_schema` para montar nomes de tabela — sem strings hardcoded. O controle de jobs permanece em SQLite (`pipeline_control.db`) e não toca o banco PostgreSQL.

**Tech Stack:** Python 3.11+, psycopg 3, FastAPI, SQLite (controle), PostgreSQL 14+ (dados)

---

## Descobertas Críticas da Análise do ZIP Real (`2025-12.zip`)

### Estrutura confirmada dos arquivos da RF

O ZIP externo `2025-12.zip` contém uma pasta `2025-12/` com ZIPs individuais:

| ZIP externo | Inner filename | Observação |
|---|---|---|
| `Cnaes.zip` | `F.K03200$Z.D51213.CNAECSV` | alias "CNAECSV" ✓ |
| `Empresas0-9.zip` | `K3241.K03200Y{N}.D51213.EMPRECSV` | alias "EMPRECSV" ✓ |
| `Estabelecimentos0-9.zip` | `K3241.K03200Y{N}.D51213.ESTABELE` | alias "ESTABELE" ✓ |
| `Socios0-9.zip` | `K3241.K03200Y{N}.D51213.SOCIOCSV` | alias "SOCIOCSV" ✓ |
| `Motivos.zip` | `F.K03200$Z.D51213.MOTICSV` | alias "MOTICSV" ✓ |
| `Municipios.zip` | `F.K03200$Z.D51213.MUNICCSV` | **BUG: alias "MUNCSV"/"MUNICSV" NÃO match "MUNICCSV"** |
| `Naturezas.zip` | `F.K03200$Z.D51213.NATJUCSV` | alias "NATJUCSV" ✓ |
| `Paises.zip` | `F.K03200$Z.D51213.PAISCSV` | alias "PAISCSV" ✓ |
| `Qualificacoes.zip` | `F.K03200$Z.D51213.QUALSCSV` | alias "QUALSCSV" ✓ |
| `Simples.zip` | `F.K03200$W.SIMPLES.CSV.D51213` | **NOVO — não está no pipeline** |
| **`Portes.zip`** | — | **NÃO existe em 2025-12** (RF parou de publicar) |

### Colunas confirmadas por amostragem real

**Empresas** (7 colunas, separador `;`, encoding `latin-1`, aspas duplas):
```
cnpj_basico ; razao_social ; natureza_juridica ; qualificacao_responsavel ; capital_social ; porte ; ente_federativo_responsavel
```

**Estabelecimentos** (30 colunas, separador `;`, encoding `latin-1`):
```
cnpj_basico ; cnpj_ordem ; cnpj_dv ; identificador_matriz_filial ; nome_fantasia ;
situacao_cadastral ; data_situacao_cadastral ; motivo_situacao_cadastral ;
nm_cidade_exterior ; pais ; data_inicio_atividade ; cnae_fiscal ;
cnae_fiscal_secundaria ; tipo_logradouro ; logradouro ; numero ; complemento ;
bairro ; cep ; uf ; municipio ; ddd1 ; telefone1 ; ddd2 ; telefone2 ;
ddd_fax ; fax ; correio_eletronico ; situacao_especial ; data_situacao_especial
```

**Socios** (11 colunas):
```
cnpj_basico ; identificador_socio ; nome_socio ; cnpj_cpf_socio ;
qualificacao_socio ; data_entrada_sociedade ; pais ; representante_legal ;
nome_representante ; qualificacao_representante ; faixa_etaria
```

**Simples** (7 colunas — confirmado por inspeção direta):
```
cnpj_basico ; opcao_pelo_simples ; data_opcao_simples ; data_exclusao_simples ;
opcao_pelo_mei ; data_opcao_mei ; data_exclusao_mei
```
Valores S/N para opções; datas no formato YYYYMMDD; "00000000" = data vazia.

---

## Estrutura Final de Schemas

```
PostgreSQL DB (corporativo compartilhado)
│
├── cnpj_staging          ← UNLOGGED, sem índices, sem constraints
│   ├── rf_empresas
│   ├── rf_estabelecimentos
│   ├── rf_socios
│   └── rf_simples        (NOVO)
│
├── cnpj                  ← dados finais tratados, com PKs e índices
│   ├── rf_empresas
│   ├── rf_estabelecimentos
│   ├── rf_socios
│   ├── rf_simples        (NOVO)
│   ├── rf_cnaes
│   ├── rf_municipios
│   ├── rf_naturezas
│   ├── rf_qualificacoes
│   ├── rf_motivos
│   ├── rf_paises
│   └── rf_portes         (mantido — opcional, RF pode não publicar todo mês)
│
└── cnpj_serving          ← materialized views e views para API
    └── mv_cnpj_full      (inclui simples: opcao_simples, opcao_mei)

SQLite: pipeline_control.db  ← não toca o banco PostgreSQL
  job_queue
  job_steps
```

---

## Task 1: Atualizar `config.py` com schemas separados e adicionar Simples

**Files:**
- Modify: `config.py`

**Contexto:** O config atual tem apenas `pg_schema = "cnpj"`. Precisamos de três schemas e adicionar "Simples" à lista de arquivos desejados.

**Step 1: Ler o arquivo atual**
```bash
# Confirmar conteúdo atual antes de editar
cat config.py
```

**Step 2: Aplicar edições**

Alterar `config.py` — adicionar após `pg_schema`:
```python
    # Postgres schemas (para banco corporativo compartilhado)
    pg_schema: str = "cnpj"                  # dados finais
    pg_staging_schema: str = "cnpj_staging"  # staging UNLOGGED (COPY rápido)
    pg_serving_schema: str = "cnpj_serving"  # materialized views para API
```

Alterar `wanted_files` para incluir "Simples" e remover "Portes" (RF parou de publicar):
```python
    wanted_files: list[str] = [
        "Empresas", "Estabelecimentos", "Socios", "Simples",
        "Cnaes", "Municipios", "Naturezas",
        "Qualificacoes", "Motivos", "Paises",
        # "Portes" removido — RF parou de publicar em 2025-12
        # Manter rf_portes no schema por compatibilidade
    ]
```

Adicionar também ao `.env.example` (documentação):
```ini
# Postgres schemas
PG_SCHEMA=cnpj
PG_STAGING_SCHEMA=cnpj_staging
PG_SERVING_SCHEMA=cnpj_serving
```

**Step 3: Verificar que o Settings carrega corretamente**
```bash
cd C:\Baixar_Ncpj
python -c "from config import settings; print(settings.pg_schema, settings.pg_staging_schema, settings.pg_serving_schema, settings.wanted_files)"
```
Esperado: `cnpj cnpj_staging cnpj_serving ['Empresas', 'Estabelecimentos', 'Socios', 'Simples', 'Cnaes', 'Municipios', 'Naturezas', 'Qualificacoes', 'Motivos', 'Paises']`

**Step 4: Commit**
```bash
git add config.py .env.example
git commit -m "feat(config): multi-schema settings + add Simples to wanted_files"
```

---

## Task 2: Reescrever `db/schema.sql` com estrutura multi-schema

**Files:**
- Modify: `db/schema.sql` (reescrita completa)

**Contexto:** Mover staging tables para `cnpj_staging`, mover `mv_cnpj_full` para `cnpj_serving`, adicionar tabelas `rf_simples`, adicionar índices em staging. Remover os `ALTER TABLE` redundantes (já cobertos pela migration).

**Step 1: Substituir o arquivo completo**

```sql
-- ============================================================
-- ETL CNPJ — PostgreSQL Schema
-- Compatível com banco corporativo compartilhado.
-- Idempotente: seguro re-executar em instalações existentes.
--
-- Run once:
--   psql $POSTGRES_URL -f db/schema.sql
-- ============================================================

-- ── Schemas ───────────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS cnpj;           -- dados finais tratados
CREATE SCHEMA IF NOT EXISTS cnpj_staging;   -- staging UNLOGGED (COPY rápido)
CREATE SCHEMA IF NOT EXISTS cnpj_serving;   -- views / materialized views para API

-- Extensão para busca por trigrama (full-text parcial)
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ── Tabelas principais: cnpj ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS cnpj.rf_empresas (
    cnpj_basico                  CHAR(8)     NOT NULL,
    razao_social                 TEXT,
    natureza_juridica            CHAR(4),
    qualificacao_responsavel     CHAR(2),
    capital_social               TEXT,
    porte                        CHAR(2),
    ente_federativo_responsavel  TEXT,
    run_key                      CHAR(7),
    created_at                   TIMESTAMPTZ DEFAULT NOW(),
    updated_at                   TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (cnpj_basico)
);

CREATE TABLE IF NOT EXISTS cnpj.rf_estabelecimentos (
    cnpj_basico                  CHAR(8)     NOT NULL,
    cnpj_ordem                   CHAR(4)     NOT NULL,
    cnpj_dv                      CHAR(2)     NOT NULL,
    identificador_matriz_filial  CHAR(1),
    nome_fantasia                TEXT,
    situacao_cadastral           CHAR(2),
    data_situacao_cadastral      CHAR(8),
    motivo_situacao_cadastral    CHAR(2),
    nm_cidade_exterior           TEXT,
    pais                         CHAR(3),
    data_inicio_atividade        CHAR(8),
    cnae_fiscal                  CHAR(7),
    cnae_fiscal_secundaria       TEXT,
    tipo_logradouro              TEXT,
    logradouro                   TEXT,
    numero                       TEXT,
    complemento                  TEXT,
    bairro                       TEXT,
    cep                          CHAR(8),
    uf                           CHAR(2),
    municipio                    CHAR(7),
    ddd1                         TEXT,
    telefone1                    TEXT,
    ddd2                         TEXT,
    telefone2                    TEXT,
    ddd_fax                      TEXT,
    fax                          TEXT,
    correio_eletronico           TEXT,
    situacao_especial            TEXT,
    data_situacao_especial       CHAR(8),
    run_key                      CHAR(7),
    created_at                   TIMESTAMPTZ DEFAULT NOW(),
    updated_at                   TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (cnpj_basico, cnpj_ordem, cnpj_dv)
);

CREATE TABLE IF NOT EXISTS cnpj.rf_socios (
    id                           BIGSERIAL   PRIMARY KEY,
    cnpj_basico                  CHAR(8),
    identificador_socio          CHAR(1),
    nome_socio                   TEXT,
    cnpj_cpf_socio               TEXT,
    qualificacao_socio           CHAR(2),
    data_entrada_sociedade       CHAR(8),
    pais                         CHAR(3),
    representante_legal          TEXT,
    nome_representante           TEXT,
    qualificacao_representante   CHAR(2),
    faixa_etaria                 CHAR(1),
    run_key                      CHAR(7),
    created_at                   TIMESTAMPTZ DEFAULT NOW(),
    updated_at                   TIMESTAMPTZ DEFAULT NOW()
);

-- Simples Nacional / MEI (NOVO — disponível a partir de 2022)
-- PK = cnpj_basico (um registro por empresa, TRUNCATE+INSERT a cada carga)
CREATE TABLE IF NOT EXISTS cnpj.rf_simples (
    cnpj_basico           CHAR(8)     NOT NULL,
    opcao_pelo_simples    CHAR(1),    -- S = optante, N = não optante
    data_opcao_simples    CHAR(8),    -- YYYYMMDD, "00000000" = não informado
    data_exclusao_simples CHAR(8),
    opcao_pelo_mei        CHAR(1),    -- S = MEI, N = não MEI
    data_opcao_mei        CHAR(8),
    data_exclusao_mei     CHAR(8),
    run_key               CHAR(7),
    updated_at            TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (cnpj_basico)
);

-- Tabelas de domínio/lookup
CREATE TABLE IF NOT EXISTS cnpj.rf_cnaes (
    codigo    CHAR(7) PRIMARY KEY,
    descricao TEXT
);

CREATE TABLE IF NOT EXISTS cnpj.rf_municipios (
    codigo    CHAR(7) PRIMARY KEY,
    descricao TEXT
);

CREATE TABLE IF NOT EXISTS cnpj.rf_naturezas (
    codigo    CHAR(4) PRIMARY KEY,
    descricao TEXT
);

CREATE TABLE IF NOT EXISTS cnpj.rf_qualificacoes (
    codigo    CHAR(2) PRIMARY KEY,
    descricao TEXT
);

CREATE TABLE IF NOT EXISTS cnpj.rf_motivos (
    codigo    CHAR(2) PRIMARY KEY,
    descricao TEXT
);

CREATE TABLE IF NOT EXISTS cnpj.rf_paises (
    codigo    CHAR(3) PRIMARY KEY,
    descricao TEXT
);

-- Portes: RF parou de publicar em 2025-12, mas mantemos a tabela
-- para retrocompatibilidade e para meses em que foi publicada.
CREATE TABLE IF NOT EXISTS cnpj.rf_portes (
    codigo    CHAR(2) PRIMARY KEY,
    descricao TEXT
);

-- ── Staging: cnpj_staging ─────────────────────────────────────────────────
-- UNLOGGED = sem WAL = COPY muito mais rápido.
-- Dados são perdidos em crash do servidor (aceitável — reprocessamos o mês).
-- Sem PKs, sem constraints = máxima velocidade no COPY.

CREATE UNLOGGED TABLE IF NOT EXISTS cnpj_staging.rf_empresas (
    cnpj_basico                  CHAR(8),
    razao_social                 TEXT,
    natureza_juridica            CHAR(4),
    qualificacao_responsavel     CHAR(2),
    capital_social               TEXT,
    porte                        CHAR(2),
    ente_federativo_responsavel  TEXT
);

CREATE UNLOGGED TABLE IF NOT EXISTS cnpj_staging.rf_estabelecimentos (
    cnpj_basico                  CHAR(8),
    cnpj_ordem                   CHAR(4),
    cnpj_dv                      CHAR(2),
    identificador_matriz_filial  CHAR(1),
    nome_fantasia                TEXT,
    situacao_cadastral           CHAR(2),
    data_situacao_cadastral      CHAR(8),
    motivo_situacao_cadastral    CHAR(2),
    nm_cidade_exterior           TEXT,
    pais                         CHAR(3),
    data_inicio_atividade        CHAR(8),
    cnae_fiscal                  CHAR(7),
    cnae_fiscal_secundaria       TEXT,
    tipo_logradouro              TEXT,
    logradouro                   TEXT,
    numero                       TEXT,
    complemento                  TEXT,
    bairro                       TEXT,
    cep                          CHAR(8),
    uf                           CHAR(2),
    municipio                    CHAR(7),
    ddd1                         TEXT,
    telefone1                    TEXT,
    ddd2                         TEXT,
    telefone2                    TEXT,
    ddd_fax                      TEXT,
    fax                          TEXT,
    correio_eletronico           TEXT,
    situacao_especial            TEXT,
    data_situacao_especial       CHAR(8)
);

CREATE UNLOGGED TABLE IF NOT EXISTS cnpj_staging.rf_socios (
    cnpj_basico                  CHAR(8),
    identificador_socio          CHAR(1),
    nome_socio                   TEXT,
    cnpj_cpf_socio               TEXT,
    qualificacao_socio           CHAR(2),
    data_entrada_sociedade       CHAR(8),
    pais                         CHAR(3),
    representante_legal          TEXT,
    nome_representante           TEXT,
    qualificacao_representante   CHAR(2),
    faixa_etaria                 CHAR(1)
);

CREATE UNLOGGED TABLE IF NOT EXISTS cnpj_staging.rf_simples (
    cnpj_basico           CHAR(8),
    opcao_pelo_simples    CHAR(1),
    data_opcao_simples    CHAR(8),
    data_exclusao_simples CHAR(8),
    opcao_pelo_mei        CHAR(1),
    data_opcao_mei        CHAR(8),
    data_exclusao_mei     CHAR(8)
);

-- Índices nas staging tables (sobrevivem ao TRUNCATE, aceleram o diff_merge)
CREATE INDEX IF NOT EXISTS idx_stg_empresas_pk
    ON cnpj_staging.rf_empresas (cnpj_basico);

CREATE INDEX IF NOT EXISTS idx_stg_estab_pk
    ON cnpj_staging.rf_estabelecimentos (cnpj_basico, cnpj_ordem, cnpj_dv);

CREATE INDEX IF NOT EXISTS idx_stg_simples_pk
    ON cnpj_staging.rf_simples (cnpj_basico);

-- ── Materialized view: cnpj_serving ───────────────────────────────────────
-- Desnormalizada para leitura rápida pela API.
-- JOIN entre estabelecimentos + empresa + lookups + simples.

CREATE MATERIALIZED VIEW IF NOT EXISTS cnpj_serving.mv_cnpj_full AS
SELECT
    -- CNPJ completo (14 dígitos concatenados)
    e.cnpj_basico || est.cnpj_ordem || est.cnpj_dv   AS cnpj_completo,
    e.cnpj_basico,
    est.cnpj_ordem,
    est.cnpj_dv,

    -- Empresa
    e.razao_social,
    e.natureza_juridica,
    nat.descricao                                      AS natureza_juridica_descricao,
    e.porte,
    prt.descricao                                      AS porte_descricao,
    e.capital_social,
    e.qualificacao_responsavel,
    e.ente_federativo_responsavel,

    -- Estabelecimento
    est.nome_fantasia,
    est.identificador_matriz_filial,
    est.situacao_cadastral,
    mot.descricao                                      AS motivo_situacao_descricao,
    est.data_situacao_cadastral,
    est.data_inicio_atividade,
    est.cnae_fiscal,
    cnae.descricao                                     AS cnae_fiscal_descricao,
    est.cnae_fiscal_secundaria,
    est.tipo_logradouro,
    est.logradouro,
    est.numero,
    est.complemento,
    est.bairro,
    est.cep,
    est.uf,
    est.municipio,
    mun.descricao                                      AS municipio_descricao,
    est.nm_cidade_exterior,
    est.pais,
    est.ddd1,
    est.telefone1,
    est.ddd2,
    est.telefone2,
    est.ddd_fax,
    est.fax,
    est.correio_eletronico,
    est.situacao_especial,
    est.data_situacao_especial,

    -- Simples Nacional / MEI
    sim.opcao_pelo_simples,
    sim.data_opcao_simples,
    sim.data_exclusao_simples,
    sim.opcao_pelo_mei,
    sim.data_opcao_mei,
    sim.data_exclusao_mei,

    -- Controle ETL
    e.run_key,
    e.updated_at
FROM cnpj.rf_estabelecimentos  est
JOIN  cnpj.rf_empresas          e    ON e.cnpj_basico   = est.cnpj_basico
LEFT JOIN cnpj.rf_cnaes         cnae ON cnae.codigo      = est.cnae_fiscal
LEFT JOIN cnpj.rf_municipios    mun  ON mun.codigo       = est.municipio
LEFT JOIN cnpj.rf_naturezas     nat  ON nat.codigo       = e.natureza_juridica
LEFT JOIN cnpj.rf_motivos       mot  ON mot.codigo       = est.motivo_situacao_cadastral
LEFT JOIN cnpj.rf_portes        prt  ON prt.codigo       = e.porte
LEFT JOIN cnpj.rf_simples       sim  ON sim.cnpj_basico  = e.cnpj_basico
WITH NO DATA;

-- ── Índices da MV ────────────────────────────────────────────────────────
-- UNIQUE obrigatório para REFRESH CONCURRENTLY

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_cnpj_completo
    ON cnpj_serving.mv_cnpj_full (cnpj_completo);

CREATE INDEX IF NOT EXISTS idx_mv_cnpj_basico
    ON cnpj_serving.mv_cnpj_full (cnpj_basico);

CREATE INDEX IF NOT EXISTS idx_mv_razao_social_trgm
    ON cnpj_serving.mv_cnpj_full USING gin (razao_social gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_mv_nome_fantasia_trgm
    ON cnpj_serving.mv_cnpj_full USING gin (nome_fantasia gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_mv_municipio
    ON cnpj_serving.mv_cnpj_full (municipio);

CREATE INDEX IF NOT EXISTS idx_mv_uf
    ON cnpj_serving.mv_cnpj_full (uf);

CREATE INDEX IF NOT EXISTS idx_mv_cnae_fiscal
    ON cnpj_serving.mv_cnpj_full (cnae_fiscal);

CREATE INDEX IF NOT EXISTS idx_mv_situacao_cadastral
    ON cnpj_serving.mv_cnpj_full (situacao_cadastral);

CREATE INDEX IF NOT EXISTS idx_mv_opcao_simples
    ON cnpj_serving.mv_cnpj_full (opcao_pelo_simples);

CREATE INDEX IF NOT EXISTS idx_mv_opcao_mei
    ON cnpj_serving.mv_cnpj_full (opcao_pelo_mei);

-- ── Índices nas tabelas base ───────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_rf_estab_cnpj_basico
    ON cnpj.rf_estabelecimentos (cnpj_basico);

CREATE INDEX IF NOT EXISTS idx_rf_socios_cnpj_basico
    ON cnpj.rf_socios (cnpj_basico);

CREATE INDEX IF NOT EXISTS idx_rf_simples_cnpj_basico
    ON cnpj.rf_simples (cnpj_basico);

-- ── Notas ─────────────────────────────────────────────────────────────────
-- Primeira carga: REFRESH MATERIALIZED VIEW cnpj_serving.mv_cnpj_full;
-- Cargas seguintes: REFRESH MATERIALIZED VIEW CONCURRENTLY cnpj_serving.mv_cnpj_full;
```

**Step 2: Verificar que SQL é válido (sem banco) — sintaxe**
```bash
# No Windows com psql disponível:
psql $POSTGRES_URL -f db/schema.sql
# Esperado: CREATE SCHEMA, CREATE TABLE, CREATE INDEX, etc. sem erros
```

**Step 3: Commit**
```bash
git add db/schema.sql
git commit -m "feat(schema): multi-schema (cnpj/cnpj_staging/cnpj_serving) + rf_simples + staging indexes"
```

---

## Task 3: Criar migration para instalações existentes

**Files:**
- Create: `db/migrations/001_multi_schema.sql`

**Contexto:** Quem já tem o pipeline rodando com o schema antigo (tudo em `cnpj`) precisa de uma migration que mova os objetos sem perder dados.

**Step 1: Criar pasta e arquivo**

```sql
-- ============================================================
-- Migration 001: Multi-schema reorganization
-- Para instalações existentes que usavam tudo no schema "cnpj".
--
-- ATENÇÃO: Faça backup antes de executar.
-- Execute uma vez: psql $POSTGRES_URL -f db/migrations/001_multi_schema.sql
-- ============================================================

BEGIN;

-- 1. Criar novos schemas
CREATE SCHEMA IF NOT EXISTS cnpj_staging;
CREATE SCHEMA IF NOT EXISTS cnpj_serving;

-- 2. Mover staging tables de cnpj → cnpj_staging
-- (só executa se ainda estiverem no schema antigo)
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'cnpj' AND table_name = 'rf_empresas_staging'
    ) THEN
        ALTER TABLE cnpj.rf_empresas_staging         SET SCHEMA cnpj_staging;
        ALTER TABLE cnpj.rf_estabelecimentos_staging SET SCHEMA cnpj_staging;
        ALTER TABLE cnpj.rf_socios_staging           SET SCHEMA cnpj_staging;
        RAISE NOTICE 'Staging tables movidas para cnpj_staging.';
    ELSE
        RAISE NOTICE 'Staging tables ja estao em cnpj_staging ou nao existem — pulando.';
    END IF;
END $$;

-- 3. Renomear staging tables para remover sufixo "_staging"
-- (nome antigo: rf_empresas_staging → novo: rf_empresas dentro de cnpj_staging)
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'cnpj_staging' AND table_name = 'rf_empresas_staging'
    ) THEN
        ALTER TABLE cnpj_staging.rf_empresas_staging         RENAME TO rf_empresas;
        ALTER TABLE cnpj_staging.rf_estabelecimentos_staging RENAME TO rf_estabelecimentos;
        ALTER TABLE cnpj_staging.rf_socios_staging           RENAME TO rf_socios;
        RAISE NOTICE 'Staging tables renomeadas (removido sufixo _staging).';
    END IF;
END $$;

-- 4. Criar staging table rf_simples (nova)
CREATE UNLOGGED TABLE IF NOT EXISTS cnpj_staging.rf_simples (
    cnpj_basico           CHAR(8),
    opcao_pelo_simples    CHAR(1),
    data_opcao_simples    CHAR(8),
    data_exclusao_simples CHAR(8),
    opcao_pelo_mei        CHAR(1),
    data_opcao_mei        CHAR(8),
    data_exclusao_mei     CHAR(8)
);

-- 5. Adicionar índices em staging (se não existirem)
CREATE INDEX IF NOT EXISTS idx_stg_empresas_pk
    ON cnpj_staging.rf_empresas (cnpj_basico);

CREATE INDEX IF NOT EXISTS idx_stg_estab_pk
    ON cnpj_staging.rf_estabelecimentos (cnpj_basico, cnpj_ordem, cnpj_dv);

CREATE INDEX IF NOT EXISTS idx_stg_simples_pk
    ON cnpj_staging.rf_simples (cnpj_basico);

-- 6. Criar tabela rf_simples no schema de dados (nova)
CREATE TABLE IF NOT EXISTS cnpj.rf_simples (
    cnpj_basico           CHAR(8)     NOT NULL,
    opcao_pelo_simples    CHAR(1),
    data_opcao_simples    CHAR(8),
    data_exclusao_simples CHAR(8),
    opcao_pelo_mei        CHAR(1),
    data_opcao_mei        CHAR(8),
    data_exclusao_mei     CHAR(8),
    run_key               CHAR(7),
    updated_at            TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (cnpj_basico)
);

CREATE INDEX IF NOT EXISTS idx_rf_simples_cnpj_basico
    ON cnpj.rf_simples (cnpj_basico);

-- 7. Mover/recriar materialized view em cnpj_serving
-- ATENÇÃO: DROP + recreate perde os dados — o próximo pipeline vai refrescar.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_matviews
        WHERE schemaname = 'cnpj' AND matviewname = 'mv_cnpj_full'
    ) THEN
        DROP MATERIALIZED VIEW cnpj.mv_cnpj_full CASCADE;
        RAISE NOTICE 'mv_cnpj_full removida de cnpj. Execute db/schema.sql para recriar em cnpj_serving.';
    END IF;
END $$;

COMMIT;

-- Execute depois:
-- psql $POSTGRES_URL -f db/schema.sql   (recria MV em cnpj_serving com nova estrutura)
-- python worker.py --once               (refaz a carga; index_step refrescará a MV)
```

**Step 2: Commit**
```bash
git add db/migrations/001_multi_schema.sql
git commit -m "feat(db): migration 001 — mover staging/MV para schemas separados"
```

---

## Task 4: Corrigir bug de alias + adicionar Simples em `transform_step.py`

**Files:**
- Modify: `steps/transform_step.py`

**Contexto:**
- BUG CRÍTICO: alias `"MUNCSV"` e `"MUNICSV"` NÃO fazem match com o inner filename real `F.K03200$Z.D51213.MUNICCSV`. Resultado: tabela de municípios nunca é carregada.
- NOVO: adicionar schema "simples" com 7 colunas.

**Step 1: Escrever teste que documenta o bug**

Criar `tests/test_transform_aliases.py`:
```python
"""Verifica que os aliases de _find_csvs batem com os nomes reais dos arquivos da RF."""
from pathlib import Path
import pytest
from steps.transform_step import TABLE_SCHEMAS, _find_csvs


# Nomes reais dos inner files extraídos dos ZIPs da RF (conforme inspeção de 2025-12)
REAL_FILENAMES = {
    "empresas":        ["K3241.K03200Y0.D51213.EMPRECSV"],
    "estabelecimentos":["K3241.K03200Y0.D51213.ESTABELE"],
    "socios":          ["K3241.K03200Y0.D51213.SOCIOCSV"],
    "simples":         ["F.K03200$W.SIMPLES.CSV.D51213"],
    "cnaes":           ["F.K03200$Z.D51213.CNAECSV"],
    "municipios":      ["F.K03200$Z.D51213.MUNICCSV"],   # BUG: alias atual não bate
    "naturezas":       ["F.K03200$Z.D51213.NATJUCSV"],
    "qualificacoes":   ["F.K03200$Z.D51213.QUALSCSV"],
    "motivos":         ["F.K03200$Z.D51213.MOTICSV"],
    "paises":          ["F.K03200$Z.D51213.PAISCSV"],
}


@pytest.mark.parametrize("table_name,filenames", REAL_FILENAMES.items())
def test_alias_matches_real_rf_filename(tmp_path: Path, table_name: str, filenames: list[str]):
    """Cada tabela deve ser encontrada pelos aliases dado o filename real da RF."""
    if table_name not in TABLE_SCHEMAS:
        pytest.skip(f"{table_name} não está em TABLE_SCHEMAS ainda")

    # Criar arquivos com os nomes reais da RF
    for fname in filenames:
        (tmp_path / fname).touch()

    schema = TABLE_SCHEMAS[table_name]
    found = _find_csvs(tmp_path, schema["filename_aliases"])
    assert len(found) == len(filenames), (
        f"Aliases {schema['filename_aliases']!r} não encontraram {filenames!r} "
        f"em '{table_name}'. Verifique os aliases no TABLE_SCHEMAS."
    )
```

**Step 2: Rodar o teste — deve FALHAR para municípios (confirma o bug)**
```bash
cd C:\Baixar_Ncpj
pytest tests/test_transform_aliases.py -v
```
Esperado: FAIL em `test_alias_matches_real_rf_filename[municipios]` + FAIL em `simples` (não existe ainda).

**Step 3: Corrigir `TABLE_SCHEMAS` em `steps/transform_step.py`**

Alterar o dict `TABLE_SCHEMAS` — duas mudanças:

```python
# 1. Corrigir alias de municipios (MUNICCSV, não MUNCSV)
"municipios": {
    "columns": ["codigo", "descricao"],
    "filename_aliases": ["Municipios", "MUNICCSV"],   # era: ["Municipios", "MUNCSV", "MUNICSV"]
    "required_columns": ["codigo"],
},

# 2. Adicionar Simples Nacional (NOVO — 7 colunas confirmadas)
"simples": {
    "columns": [
        "cnpj_basico", "opcao_pelo_simples", "data_opcao_simples",
        "data_exclusao_simples", "opcao_pelo_mei", "data_opcao_mei",
        "data_exclusao_mei",
    ],
    "filename_aliases": ["Simples", "SIMPLES"],
    "required_columns": ["cnpj_basico"],
},
```

**Step 4: Rodar testes — todos devem PASSAR**
```bash
pytest tests/test_transform_aliases.py -v
```
Esperado: PASS em todos.

**Step 5: Commit**
```bash
git add steps/transform_step.py tests/test_transform_aliases.py
git commit -m "fix(transform): corrigir alias MUNICCSV + adicionar schema simples (7 colunas RF)"
```

---

## Task 5: Reescrever `steps/load_step.py` com schemas separados + Simples

**Files:**
- Modify: `steps/load_step.py`

**Contexto:** Maior mudança. Substituir todas as strings hardcoded `"cnpj.rf_..."` por `f"{D}.rf_..."` onde `D = settings.pg_schema`, `S = settings.pg_staging_schema`. Adicionar lógica de carga para `rf_simples` (TRUNCATE + INSERT, igual a sócios — sem PK natural estável para diff_merge).

**Step 1: Substituições no topo do arquivo**

```python
from config import settings

# ── Schemas ────────────────────────────────────────────────────────────────
_D = settings.pg_schema          # cnpj          — dados finais
_S = settings.pg_staging_schema  # cnpj_staging  — tabelas UNLOGGED
```

**Step 2: Substituir colunas e constantes de Simples — adicionar após `_SOCIOS_COLS`:**

```python
_SIMPLES_COLS: list[str] = [
    "cnpj_basico", "opcao_pelo_simples", "data_opcao_simples",
    "data_exclusao_simples", "opcao_pelo_mei", "data_opcao_mei",
    "data_exclusao_mei",
]
```

**Step 3: Substituir `LOOKUP_TABLE_MAP` — sem strings hardcoded:**

```python
LOOKUP_TABLE_MAP: dict[str, str] = {
    "cnaes":         f"{_D}.rf_cnaes",
    "municipios":    f"{_D}.rf_municipios",
    "naturezas":     f"{_D}.rf_naturezas",
    "qualificacoes": f"{_D}.rf_qualificacoes",
    "motivos":       f"{_D}.rf_motivos",
    "paises":        f"{_D}.rf_paises",
    "portes":        f"{_D}.rf_portes",
}
```

**Step 4: Adicionar função `_replace_simples` após `_replace_socios`:**

```python
def _replace_simples(conn: psycopg.Connection, run_key: str) -> int:
    """
    Simples: TRUNCATE main + INSERT from staging.
    A RF republica o arquivo completo todo mês — não há deleções parciais.
    """
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {_D}.rf_simples")

    cols = ", ".join(f'"{c}"' for c in _SIMPLES_COLS)
    s_cols = ", ".join(f"s.{c}" for c in _SIMPLES_COLS)
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO {_D}.rf_simples ({cols}, run_key, updated_at)
            SELECT {s_cols}, %(run_key)s, NOW()
            FROM {_S}.rf_simples s
            WHERE NULLIF(BTRIM(s.cnpj_basico), '') IS NOT NULL
        """, {"run_key": run_key})
        return cur.rowcount
```

**Step 5: Atualizar todas as referências a tabelas dentro de `run()` e helpers:**

Substituições (busca e replace, sem exceção):
```python
# _stage():
"cnpj.rf_empresas_staging"         → f"{_S}.rf_empresas"
"cnpj.rf_estabelecimentos_staging" → f"{_S}.rf_estabelecimentos"
"cnpj.rf_socios_staging"           → f"{_S}.rf_socios"

# _diff_merge() chamadas:
"cnpj.rf_empresas"                 → f"{_D}.rf_empresas"
"cnpj.rf_estabelecimentos"         → f"{_D}.rf_estabelecimentos"

# _replace_socios():
"cnpj.rf_socios RESTART IDENTITY"  → f"{_D}.rf_socios RESTART IDENTITY"
"cnpj.rf_socios_staging"           → f"{_S}.rf_socios"
"cnpj.rf_socios"  (INSERT INTO)    → f"{_D}.rf_socios"
```

**Step 6: Adicionar bloco para Simples no `run()` — após o bloco `elif tbl_key == "socios":`:**

```python
elif tbl_key == "simples":
    _stage(conn, f"{_S}.rf_simples", csv_path, _SIMPLES_COLS)
    count = _replace_simples(conn, run_key)
    conn.commit()
    log.info(f"rf_simples — replaced with {count:,} rows")
    results.append({
        "table": f"{_D}.rf_simples",
        "inserted": count, "updated": 0, "deleted": 0,
    })
```

**Step 7: Atualizar `_check_staging_volume` para usar schemas corretos (da Task anterior de fase 1)**

Garantir que as referências usem `_S` e `_D`.

**Step 8: Verificar que não sobrou nenhuma string `"cnpj."` hardcoded:**
```bash
grep -n '"cnpj\.' steps/load_step.py
```
Esperado: zero resultados.

**Step 9: Rodar testes existentes**
```bash
pytest tests/ -v
```

**Step 10: Commit**
```bash
git add steps/load_step.py
git commit -m "feat(load): multi-schema (cnpj_staging/cnpj) + add Simples load logic"
```

---

## Task 6: Atualizar `steps/index_step.py` para `cnpj_serving.mv_cnpj_full`

**Files:**
- Modify: `steps/index_step.py`

**Step 1: Adicionar import e variável de schema no topo:**

```python
from config import settings

_V = settings.pg_serving_schema  # cnpj_serving
```

**Step 2: Substituir todas as referências à MV:**

```python
# _view_has_rows():
# Antes:
"WHERE schemaname = 'cnpj' AND matviewname = 'mv_cnpj_full'"
# Depois:
f"WHERE schemaname = '{_V}' AND matviewname = 'mv_cnpj_full'"

# run():
# Antes:
conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY cnpj.mv_cnpj_full")
conn.execute("REFRESH MATERIALIZED VIEW cnpj.mv_cnpj_full")
conn.execute("SELECT COUNT(*) FROM cnpj.mv_cnpj_full")
# Depois:
conn.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {_V}.mv_cnpj_full")
conn.execute(f"REFRESH MATERIALIZED VIEW {_V}.mv_cnpj_full")
conn.execute(f"SELECT COUNT(*) FROM {_V}.mv_cnpj_full")
```

**Step 3: Verificar sem `"cnpj."` hardcoded:**
```bash
grep -n '"cnpj\.' steps/index_step.py
```
Esperado: zero.

**Step 4: Commit**
```bash
git add steps/index_step.py
git commit -m "feat(index): use cnpj_serving.mv_cnpj_full via settings.pg_serving_schema"
```

---

## Task 7: Atualizar rotas da API para `cnpj_serving.mv_cnpj_full`

**Files:**
- Modify: `api/routes/cnpj.py`
- Modify: `api/routes/search.py`

**Contexto:** As queries ainda apontam para `cnpj.mv_cnpj_full`. Precisam apontar para `cnpj_serving.mv_cnpj_full`. Usaremos uma constante `_MV` definida a partir de `settings`.

**Step 1: `api/routes/cnpj.py` — adicionar import e constante:**

```python
from config import settings

_MV = f"{settings.pg_serving_schema}.mv_cnpj_full"

_SELECT_14 = f"SELECT * FROM {_MV} WHERE cnpj_completo = %s LIMIT 1"
_SELECT_8  = f"SELECT * FROM {_MV} WHERE cnpj_basico = %s ORDER BY cnpj_ordem LIMIT 100"
```

**Step 2: `api/routes/search.py` — adicionar import e constante:**

```python
from config import settings

_MV = f"{settings.pg_serving_schema}.mv_cnpj_full"
```

E atualizar a query dentro de `search()`:
```python
# Antes:
query = f"SELECT * FROM cnpj.mv_cnpj_full {where} LIMIT %s"
# Depois:
query = f"SELECT * FROM {_MV} {where} LIMIT %s"
```

**Step 3: Verificar sem `"cnpj."` hardcoded:**
```bash
grep -n '"cnpj\.' api/routes/cnpj.py api/routes/search.py
```
Esperado: zero.

**Step 4: Commit**
```bash
git add api/routes/cnpj.py api/routes/search.py
git commit -m "feat(api): use cnpj_serving.mv_cnpj_full via settings.pg_serving_schema"
```

---

## Task 8: Atualizar `api/schemas.py` com campos Simples

**Files:**
- Modify: `api/schemas.py`

**Contexto:** `CNPJResponse` precisa incluir os novos campos de Simples que a MV agora retorna.

**Step 1: Adicionar campos ao final de `CNPJResponse`, antes de `run_key`:**

```python
class CNPJResponse(BaseModel):
    cnpj_completo: str
    cnpj_basico: str
    # ... campos existentes mantidos sem alteração ...
    correio_eletronico: Optional[str] = None
    identificador_matriz_filial: Optional[str] = None

    # Simples Nacional / MEI
    opcao_pelo_simples: Optional[str] = None    # "S" ou "N"
    data_opcao_simples: Optional[str] = None
    data_exclusao_simples: Optional[str] = None
    opcao_pelo_mei: Optional[str] = None        # "S" ou "N"
    data_opcao_mei: Optional[str] = None
    data_exclusao_mei: Optional[str] = None

    run_key: Optional[str] = None
```

**Step 2: Adicionar também campos extras da MV que estavam faltando:**
```python
    # Campos do estabelecimento que faltavam no schema anterior
    situacao_especial: Optional[str] = None
    data_situacao_especial: Optional[str] = None
    nm_cidade_exterior: Optional[str] = None
    pais: Optional[str] = None
    ddd2: Optional[str] = None
    telefone2: Optional[str] = None
    ddd_fax: Optional[str] = None
    fax: Optional[str] = None
    qualificacao_responsavel: Optional[str] = None
    ente_federativo_responsavel: Optional[str] = None
    data_situacao_cadastral: Optional[str] = None
```

**Step 3: Commit**
```bash
git add api/schemas.py
git commit -m "feat(schemas): add Simples/MEI fields + complete MV field coverage in CNPJResponse"
```

---

## Task 9: Atualizar `steps/cleanup_step.py` para remover referência à MV antiga

**Files:**
- Modify: `steps/cleanup_step.py`

**Contexto:** A função `run()` já não referencia a MV diretamente (só escreve `ultimo_status.json`), mas a mensagem hardcoded "SUCCESS" deve ser mantida. Nenhuma alteração funcional necessária — apenas adicionar o import correto de `settings` se ainda não estiver presente.

Verificar:
```bash
grep -n '"cnpj\.' steps/cleanup_step.py
```
Se zero resultados, nenhuma alteração necessária.

---

## Task 10: Verificação final end-to-end

**Step 1: Aplicar schema em banco de teste**
```bash
psql $POSTGRES_URL -f db/schema.sql
```
Esperado: sem erros; schemas `cnpj`, `cnpj_staging`, `cnpj_serving` criados.

**Step 2: Verificar que schemas existem**
```bash
psql $POSTGRES_URL -c "\dn" | grep cnpj
```
Esperado:
```
cnpj
cnpj_serving
cnpj_staging
```

**Step 3: Verificar tabelas por schema**
```bash
psql $POSTGRES_URL -c "\dt cnpj.*"
psql $POSTGRES_URL -c "\dt cnpj_staging.*"
psql $POSTGRES_URL -c "\dm cnpj_serving.*"
```

**Step 4: Rodar suite de testes completa**
```bash
pytest tests/ -v --tb=short
```

**Step 5: Rodar dry_run com um arquivo de cada tipo**
```bash
python dry_run_local.py --max-files 2
```
Verificar nos logs:
- `[transform]` processou "simples" → X rows
- `[transform]` processou "municipios" → X rows (antes ficava 0 por bug de alias)

**Step 6: Verificar que não há strings hardcoded `"cnpj."` no código Python**
```bash
grep -rn '"cnpj\.' steps/ api/ --include="*.py"
```
Esperado: zero resultados.

**Step 7: Commit final**
```bash
git add -A
git commit -m "feat: multi-schema refactor complete + Simples Nacional + fix alias MUNICCSV"
```

---

## Resumo das Mudanças por Arquivo

| Arquivo | Tipo | O que muda |
|---|---|---|
| `config.py` | Modificar | `pg_staging_schema`, `pg_serving_schema`, `Simples` em `wanted_files` |
| `.env.example` | Modificar | Documentar novos schemas |
| `db/schema.sql` | Reescrever | 3 schemas, rf_simples, staging renomeado, MV em cnpj_serving |
| `db/migrations/001_multi_schema.sql` | Criar | Migration para instalações existentes |
| `steps/transform_step.py` | Modificar | Fix alias MUNICCSV, adicionar schema "simples" |
| `steps/load_step.py` | Modificar | Schemas dinâmicos via settings, lógica de carga Simples |
| `steps/index_step.py` | Modificar | Referência `cnpj_serving.mv_cnpj_full` |
| `api/routes/cnpj.py` | Modificar | Referência `cnpj_serving.mv_cnpj_full` |
| `api/routes/search.py` | Modificar | Referência `cnpj_serving.mv_cnpj_full` |
| `api/schemas.py` | Modificar | Campos Simples/MEI + campos extras da MV |
| `tests/test_transform_aliases.py` | Criar | Testa aliases contra nomes reais de arquivo RF |

## Boas Práticas para Banco Corporativo Compartilhado

1. **Nunca criar objetos em `public`** — todos os objetos sob `cnpj`, `cnpj_staging` ou `cnpj_serving`.
2. **Prefixo `cnpj_` nos schemas derivados** — evita conflito com schemas genéricos de outros projetos (`staging`, `serving` sem prefixo colidiriam).
3. **Grants mínimos** — criar um role específico (`cnpj_etl`) com acesso apenas aos três schemas.
4. **Schemas via config, nunca hardcoded** — permite rodar em banco de dev/homolog/prod com schemas diferentes sem alterar código.
5. **UNLOGGED isolado em schema próprio** — se o DBA quiser excluir o staging, não afeta dados finais.
