-- Migration 006: índice para /socios/buscar — busca por CPF parcial + nome
--
-- Problema: a query filtra rf_socios por (cnpj_cpf_socio, nome_socio) mas só
-- existia índice em cnpj_basico → full scan → statement timeout de 8s.
--
-- Aplica: psql $POSTGRES_URL -f db/migrations/006_socios_buscar_index.sql
--
-- CONCURRENTLY: não trava a tabela durante a criação (pode demorar alguns minutos).

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_rf_socios_cpf_nome
    ON cnpj.rf_socios (cnpj_cpf_socio, nome_socio);
