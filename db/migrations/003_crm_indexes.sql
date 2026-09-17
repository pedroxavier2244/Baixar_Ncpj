-- Migration 003: índices otimizados para uso em CRM
--
-- Aplica: psql $POSTGRES_URL -f db/migrations/003_crm_indexes.sql
--
-- Índice parcial para empresas ATIVAS — filtro mais comum do CRM.
-- Partial index: só indexa situacao_cadastral = '02', muito menor que índice total.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_mv_ativas_uf_cnae
    ON cnpj_serving.mv_cnpj_full (uf, cnae_fiscal)
    WHERE situacao_cadastral = '02';

-- Índice para filtro "apenas matriz" combinado com UF
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_mv_matriz_uf
    ON cnpj_serving.mv_cnpj_full (uf, situacao_cadastral)
    WHERE identificador_matriz_filial = '1';

-- Índice para filtro "tem telefone" — telemarketing/CRM
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_mv_tem_telefone
    ON cnpj_serving.mv_cnpj_full (uf, cnae_fiscal)
    WHERE ddd1 IS NOT NULL AND telefone1 IS NOT NULL AND telefone1 <> '';

-- Índice por porte (segmentação por tamanho de empresa)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_mv_porte
    ON cnpj_serving.mv_cnpj_full (porte, situacao_cadastral);
