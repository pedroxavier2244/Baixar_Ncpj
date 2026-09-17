-- ============================================================
-- Migration 005: suporte à estratégia de schema swap
-- Para instalações existentes com dados carregados via diff-merge.
--
-- Esta migration NÃO destrói dados existentes.
-- Apenas garante que as colunas de identidade e defaults estão corretos.
--
-- Execute UMA VEZ antes do próximo run mensal:
--   psql $POSTGRES_URL -f db/migrations/005_schema_swap.sql
-- ============================================================

BEGIN;

-- 1. Garantir que run_key/created_at/updated_at existem (seguro se já existem)
ALTER TABLE cnpj.rf_empresas
    ADD COLUMN IF NOT EXISTS run_key    CHAR(7),
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

ALTER TABLE cnpj.rf_estabelecimentos
    ADD COLUMN IF NOT EXISTS run_key    CHAR(7),
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

ALTER TABLE cnpj.rf_socios
    ADD COLUMN IF NOT EXISTS run_key    CHAR(7),
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

ALTER TABLE cnpj.rf_simples
    ADD COLUMN IF NOT EXISTS run_key    CHAR(7),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

-- 2. Garantir que cnpj_serving existe (setup antigo pode não ter)
CREATE SCHEMA IF NOT EXISTS cnpj_serving;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 3. Recriar MV em cnpj_serving se ainda estiver em cnpj
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_matviews
        WHERE schemaname = 'cnpj' AND matviewname = 'mv_cnpj_full'
    ) AND NOT EXISTS (
        SELECT 1 FROM pg_matviews
        WHERE schemaname = 'cnpj_serving' AND matviewname = 'mv_cnpj_full'
    ) THEN
        -- Mover MV para cnpj_serving
        ALTER MATERIALIZED VIEW cnpj.mv_cnpj_full SET SCHEMA cnpj_serving;
        RAISE NOTICE 'mv_cnpj_full movida para cnpj_serving.';
    ELSE
        RAISE NOTICE 'mv_cnpj_full já em cnpj_serving ou não existe em cnpj — sem ação.';
    END IF;
END $$;

COMMIT;

-- Próximo passo: rodar o pipeline normalmente.
-- O load_step vai:
--   1. Dropar qualquer _old table existente (CASCADE remove MV obsoleta se houver)
--   2. Construir _new tables e fazer o swap
-- O index_step vai:
--   1. Recriar o MV se necessário
--   2. Fazer REFRESH
--   3. Dropar as _old tables com segurança
