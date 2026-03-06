-- ============================================================
-- Migration 001: Multi-schema reorganization
-- Para instalacoes existentes que usavam tudo no schema "cnpj".
--
-- ATENCAO: Faca backup antes de executar.
-- Execute uma vez: psql $POSTGRES_URL -f db/migrations/001_multi_schema.sql
-- Depois execute: psql $POSTGRES_URL -f db/schema.sql
-- ============================================================

BEGIN;

-- 1. Criar novos schemas
CREATE SCHEMA IF NOT EXISTS cnpj_staging;
CREATE SCHEMA IF NOT EXISTS cnpj_serving;

-- 2. Mover staging tables de cnpj -> cnpj_staging (se ainda estiverem la)
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
        RAISE NOTICE 'Staging tables ja em cnpj_staging ou nao existem -- pulando.';
    END IF;
END $$;

-- 3. Renomear staging tables (remover sufixo _staging)
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'cnpj_staging' AND table_name = 'rf_empresas_staging'
    ) THEN
        ALTER TABLE cnpj_staging.rf_empresas_staging         RENAME TO rf_empresas;
        ALTER TABLE cnpj_staging.rf_estabelecimentos_staging RENAME TO rf_estabelecimentos;
        ALTER TABLE cnpj_staging.rf_socios_staging           RENAME TO rf_socios;
        RAISE NOTICE 'Staging tables renomeadas (sufixo _staging removido).';
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

-- 5. Indices nas staging tables
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

-- 7. Remover MV antiga do schema cnpj (sera recriada em cnpj_serving por db/schema.sql)
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_matviews
        WHERE schemaname = 'cnpj' AND matviewname = 'mv_cnpj_full'
    ) THEN
        DROP MATERIALIZED VIEW cnpj.mv_cnpj_full CASCADE;
        RAISE NOTICE 'mv_cnpj_full removida de cnpj. Execute db/schema.sql para recriar em cnpj_serving.';
    ELSE
        RAISE NOTICE 'mv_cnpj_full nao encontrada em cnpj -- nada a remover.';
    END IF;
END $$;

COMMIT;

-- Proximo passo apos executar esta migration:
-- psql $POSTGRES_URL -f db/schema.sql
-- python worker.py --once   (ou python setup.py)
