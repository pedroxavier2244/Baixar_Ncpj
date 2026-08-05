-- ============================================================
-- Migration 002: Índice trigram em municipio_descricao
-- Corrige busca /search?municipio= que fazia full scan em 70M linhas.
--
-- Execute uma vez no servidor:
--   psql $POSTGRES_URL -f db/migrations/002_municipio_descricao_trgm.sql
--
-- Tempo estimado: 2-5 minutos (cria índice em 70M linhas sem travar leituras)
-- ============================================================

-- pg_trgm já foi criada em schema.sql, mas garantimos aqui por segurança
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- CONCURRENTLY = não trava leituras/queries enquanto o índice é construído
-- A API continua respondendo normalmente durante a criação
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_mv_municipio_descricao_trgm
    ON cnpj_serving.mv_cnpj_full USING gin (municipio_descricao gin_trgm_ops);
