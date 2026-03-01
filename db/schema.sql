-- ============================================================
-- ETL CNPJ — PostgreSQL Schema
-- Run once: psql $POSTGRES_URL -f db/schema.sql
-- ============================================================

CREATE SCHEMA IF NOT EXISTS cnpj;

-- Enable trigram extension for full-text search
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ── Source tables ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS cnpj.rf_empresas (
    cnpj_basico                  CHAR(8)        NOT NULL,
    razao_social                 TEXT,
    natureza_juridica            CHAR(4),
    qualificacao_responsavel     CHAR(2),
    capital_social               TEXT,
    porte                        CHAR(2),
    ente_federativo_responsavel  TEXT,
    run_key                      CHAR(7),
    PRIMARY KEY (cnpj_basico)
);

CREATE TABLE IF NOT EXISTS cnpj.rf_estabelecimentos (
    cnpj_basico                  CHAR(8)        NOT NULL,
    cnpj_ordem                   CHAR(4)        NOT NULL,
    cnpj_dv                      CHAR(2)        NOT NULL,
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
    PRIMARY KEY (cnpj_basico, cnpj_ordem, cnpj_dv)
);

CREATE TABLE IF NOT EXISTS cnpj.rf_socios (
    id                           BIGSERIAL      PRIMARY KEY,
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
    run_key                      CHAR(7)
);

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

CREATE TABLE IF NOT EXISTS cnpj.rf_portes (
    codigo    CHAR(2) PRIMARY KEY,
    descricao TEXT
);

-- ── Materialized view for fast querying ──────────────────────

CREATE MATERIALIZED VIEW IF NOT EXISTS cnpj.mv_cnpj_full AS
SELECT
    e.cnpj_basico || est.cnpj_ordem || est.cnpj_dv  AS cnpj_completo,
    e.cnpj_basico,
    est.cnpj_ordem,
    est.cnpj_dv,
    e.razao_social,
    est.nome_fantasia,
    e.natureza_juridica,
    nat.descricao                                    AS natureza_juridica_descricao,
    e.porte,
    prt.descricao                                    AS porte_descricao,
    e.capital_social,
    est.situacao_cadastral,
    mot.descricao                                    AS motivo_situacao_descricao,
    est.data_inicio_atividade,
    est.cnae_fiscal,
    cnae.descricao                                   AS cnae_fiscal_descricao,
    est.cnae_fiscal_secundaria,
    est.tipo_logradouro,
    est.logradouro,
    est.numero,
    est.complemento,
    est.bairro,
    est.cep,
    est.uf,
    est.municipio,
    mun.descricao                                    AS municipio_descricao,
    est.ddd1,
    est.telefone1,
    est.correio_eletronico,
    est.identificador_matriz_filial,
    e.run_key
FROM cnpj.rf_estabelecimentos  est
JOIN cnpj.rf_empresas           e    ON e.cnpj_basico   = est.cnpj_basico
LEFT JOIN cnpj.rf_cnaes         cnae ON cnae.codigo      = est.cnae_fiscal
LEFT JOIN cnpj.rf_municipios    mun  ON mun.codigo       = est.municipio
LEFT JOIN cnpj.rf_naturezas     nat  ON nat.codigo       = e.natureza_juridica
LEFT JOIN cnpj.rf_motivos       mot  ON mot.codigo       = est.motivo_situacao_cadastral
LEFT JOIN cnpj.rf_portes        prt  ON prt.codigo       = e.porte
WITH NO DATA;

-- ── Indexes ───────────────────────────────────────────────────

-- Primary lookup by full CNPJ (14 digits)
CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_cnpj_completo
    ON cnpj.mv_cnpj_full (cnpj_completo);

-- Lookup by base CNPJ (8 digits) — returns all establishments
CREATE INDEX IF NOT EXISTS idx_mv_cnpj_basico
    ON cnpj.mv_cnpj_full (cnpj_basico);

-- Full-text search on razao_social and nome_fantasia (trigram)
CREATE INDEX IF NOT EXISTS idx_mv_razao_social_trgm
    ON cnpj.mv_cnpj_full USING gin (razao_social gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_mv_nome_fantasia_trgm
    ON cnpj.mv_cnpj_full USING gin (nome_fantasia gin_trgm_ops);

-- Filter indexes
CREATE INDEX IF NOT EXISTS idx_mv_municipio
    ON cnpj.mv_cnpj_full (municipio);

CREATE INDEX IF NOT EXISTS idx_mv_uf
    ON cnpj.mv_cnpj_full (uf);

CREATE INDEX IF NOT EXISTS idx_mv_cnae_fiscal
    ON cnpj.mv_cnpj_full (cnae_fiscal);

CREATE INDEX IF NOT EXISTS idx_mv_situacao_cadastral
    ON cnpj.mv_cnpj_full (situacao_cadastral);

-- Source table foreign-key support indexes
CREATE INDEX IF NOT EXISTS idx_rf_estab_cnpj_basico
    ON cnpj.rf_estabelecimentos (cnpj_basico);

CREATE INDEX IF NOT EXISTS idx_rf_socios_cnpj_basico
    ON cnpj.rf_socios (cnpj_basico);

-- ── Helper: populate materialized view (run after first load) ─
-- REFRESH MATERIALIZED VIEW CONCURRENTLY cnpj.mv_cnpj_full;
-- Note: CONCURRENTLY requires a unique index (idx_mv_cnpj_completo) to exist first.
-- On the very first refresh use: REFRESH MATERIALIZED VIEW cnpj.mv_cnpj_full;
