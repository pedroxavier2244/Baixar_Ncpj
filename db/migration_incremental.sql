-- ============================================================
-- Migration: incremental merge support
-- Run once on existing installations that already have data.
-- psql $POSTGRES_URL -f db/migration_incremental.sql
-- ============================================================

-- 1. Add timestamp columns to data tables (safe if already exist)
ALTER TABLE cnpj.rf_empresas
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

ALTER TABLE cnpj.rf_estabelecimentos
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

ALTER TABLE cnpj.rf_socios
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

-- 2. Staging tables (created by schema.sql, harmless if already exist)
CREATE UNLOGGED TABLE IF NOT EXISTS cnpj.rf_empresas_staging (
    cnpj_basico                  CHAR(8),
    razao_social                 TEXT,
    natureza_juridica            CHAR(4),
    qualificacao_responsavel     CHAR(2),
    capital_social               TEXT,
    porte                        CHAR(2),
    ente_federativo_responsavel  TEXT
);

CREATE UNLOGGED TABLE IF NOT EXISTS cnpj.rf_estabelecimentos_staging (
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

CREATE UNLOGGED TABLE IF NOT EXISTS cnpj.rf_socios_staging (
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

-- 3. Add updated_at to materialized view (requires DROP + recreate)
-- Only needed if mv_cnpj_full already exists without updated_at.
-- Uncomment and run manually if needed:
-- DROP MATERIALIZED VIEW IF EXISTS cnpj.mv_cnpj_full CASCADE;
-- Then re-run db/schema.sql to recreate it with updated_at.
