-- Migration 007: índice trigram em razao_social e nome_fantasia
--
-- Problema: /search?razao_social=... usa ILIKE '%termo%' (wildcard dos dois lados).
-- Sem índice trigram, o PostgreSQL faz full sequential scan na mv_cnpj_full
-- (50M+ linhas) → statement timeout de 8s.
--
-- Solução: GIN trigram index em razao_social e nome_fantasia.
-- Requer extensão pg_trgm (já criada em schema.sql).
--
-- Aplica: psql $POSTGRES_URL -f db/migrations/007_razao_social_trgm_index.sql
--
-- CONCURRENTLY: não trava leituras durante a criação (pode demorar alguns minutos).

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_mv_razao_social_trgm
    ON cnpj_serving.mv_cnpj_full USING gin (razao_social gin_trgm_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_mv_nome_fantasia_trgm
    ON cnpj_serving.mv_cnpj_full USING gin (nome_fantasia gin_trgm_ops);
