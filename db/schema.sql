-- ============================================================
-- ETL CNPJ — PostgreSQL Schema
-- Compativel com banco corporativo compartilhado.
-- Idempotente: seguro re-executar em instalacoes existentes.
--
-- Run once:
--   psql $POSTGRES_URL -f db/schema.sql
-- ============================================================

-- ── Schemas ───────────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS cnpj;           -- dados finais tratados
CREATE SCHEMA IF NOT EXISTS cnpj_staging;   -- staging UNLOGGED (COPY rapido)
CREATE SCHEMA IF NOT EXISTS cnpj_serving;   -- views / materialized views para API

-- Extensao para busca por trigrama (full-text parcial)
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

-- Simples Nacional / MEI
-- PK = cnpj_basico (um registro por empresa, TRUNCATE+INSERT a cada carga)
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

-- Tabelas de dominio/lookup
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

-- Portes: RF pode nao publicar todo mes, mas mantemos a tabela para retrocompatibilidade
CREATE TABLE IF NOT EXISTS cnpj.rf_portes (
    codigo    CHAR(2) PRIMARY KEY,
    descricao TEXT
);

-- ── Staging: cnpj_staging ─────────────────────────────────────────────────
-- UNLOGGED = sem WAL = COPY muito mais rapido.
-- Dados perdidos em crash do servidor (aceitavel — reprocessamos o mes).
-- Sem PKs, sem constraints = maxima velocidade no COPY.

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

-- Indices nas staging tables (sobrevivem ao TRUNCATE, aceleram o diff_merge)
CREATE INDEX IF NOT EXISTS idx_stg_empresas_pk
    ON cnpj_staging.rf_empresas (cnpj_basico);

CREATE INDEX IF NOT EXISTS idx_stg_estab_pk
    ON cnpj_staging.rf_estabelecimentos (cnpj_basico, cnpj_ordem, cnpj_dv);

CREATE INDEX IF NOT EXISTS idx_stg_simples_pk
    ON cnpj_staging.rf_simples (cnpj_basico);

-- ── Materialized view: cnpj_serving ───────────────────────────────────────

CREATE MATERIALIZED VIEW IF NOT EXISTS cnpj_serving.mv_cnpj_full AS
SELECT
    e.cnpj_basico || est.cnpj_ordem || est.cnpj_dv   AS cnpj_completo,
    e.cnpj_basico,
    est.cnpj_ordem,
    est.cnpj_dv,
    e.razao_social,
    e.natureza_juridica,
    nat.descricao                                      AS natureza_juridica_descricao,
    e.porte,
    prt.descricao                                      AS porte_descricao,
    e.capital_social,
    e.qualificacao_responsavel,
    e.ente_federativo_responsavel,
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
    sim.opcao_pelo_simples,
    sim.data_opcao_simples,
    sim.data_exclusao_simples,
    sim.opcao_pelo_mei,
    sim.data_opcao_mei,
    sim.data_exclusao_mei,
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

-- ── Indices da MV ─────────────────────────────────────────────────────────
-- UNIQUE obrigatorio para REFRESH CONCURRENTLY

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

-- ── Indices nas tabelas base ───────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_rf_estab_cnpj_basico
    ON cnpj.rf_estabelecimentos (cnpj_basico);

CREATE INDEX IF NOT EXISTS idx_rf_socios_cnpj_basico
    ON cnpj.rf_socios (cnpj_basico);

CREATE INDEX IF NOT EXISTS idx_rf_simples_cnpj_basico
    ON cnpj.rf_simples (cnpj_basico);

-- ── Notas ─────────────────────────────────────────────────────────────────
-- Primeira carga: REFRESH MATERIALIZED VIEW cnpj_serving.mv_cnpj_full;
-- Cargas seguintes: REFRESH MATERIALIZED VIEW CONCURRENTLY cnpj_serving.mv_cnpj_full;
