-- Migration 006: índice para /socios/buscar — busca por CPF parcial + nome
--
-- Problema: a query filtra rf_socios por (cnpj_cpf_socio, nome_socio) mas só
-- existia índice em cnpj_basico → full scan → statement timeout de 8s.
--
-- Aplica: psql $POSTGRES_URL -f db/migrations/006_socios_buscar_index.sql
--
-- CONCURRENTLY: não trava a tabela durante a criação (pode demorar alguns minutos).
--
-- ⚠️  24/08/2026 — ESTE ARQUIVO SOZINHO NAO SEGURA O INDICE.
-- Ele foi aplicado, e mesmo assim o indice foi encontrado AUSENTE em 24/08/2026:
-- o load_step reconstroi rf_socios do zero a cada carga da RF (monta a _new e
-- renomeia por cima), e leva junto todo indice que nao esteja no index_sqls de
-- steps/load_step.py. Quem garante a existencia do indice e aquela lista, nao esta
-- migration — que serve so pra criar o indice ENTRE cargas, sem esperar a proxima.
-- Custo de nao ter: 3,1s por consulta em seq scan sobre 28M linhas, contra 8,9ms
-- com indice (medido na VPS em 24/08/2026).
--
-- O NOME MUDOU, e o motivo importa: era idx_rf_socios_cpf_nome. O load_step faz
-- DROP INDEX IF EXISTS <nome> antes de criar, e so acha o que tiver o nome exato
-- que ele proprio usa. Com nomes diferentes, rodar esta migration depois de uma
-- carga criava um SEGUNDO indice identico na mesma tabela (1178 MB a mais, e os
-- dois mantidos a cada escrita). Com o nome alinhado, rodar de novo e no-op.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_rf_socios_new_cpf_nome
    ON cnpj.rf_socios (cnpj_cpf_socio, nome_socio);
