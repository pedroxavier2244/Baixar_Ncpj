-- Migration 002: índice trigram em municipio_descricao
-- Necessário para /search?municipio=... não fazer varredura completa na MV
--
-- Requer: extensão pg_trgm (já criada em schema.sql)
-- Aplica: psql $POSTGRES_URL -f db/migrations/002_perf_indexes.sql

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_mv_municipio_descricao_trgm
    ON cnpj_serving.mv_cnpj_full USING gin (municipio_descricao gin_trgm_ops);
