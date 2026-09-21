-- Migration 010: socio.job_controle — memória do job entre execuções
--
-- Nasceu de uma pergunta certa: a carga da RF leva 4-5h e começa às 3h; o job
-- rodava às 4h, ou seja, no meio dela. Duas coisas quebravam:
--
--   1) O load_step DROPA o índice do reverso da tabela VIVA antes de recriá-lo
--      na _new (é o DROP INDEX IF EXISTS do load_step.py:337 — o nome do índice
--      sobrevive ao rename, então ele encontra o da tabela live). Durante essa
--      janela o reverso vira seq scan sobre 28M linhas e o job passaria de
--      3min37s para horas.
--
--   2) O swap final (ALTER TABLE ... RENAME) precisa de ACCESS EXCLUSIVE. Uma
--      query longa do job segurando rf_socios faria o ALTER esperar por ela —
--      o job atrasaria a carga da RF, não o contrário.
--
-- E uma terceira, que não é de concorrência e sim de correção: depois de uma
-- carga nova, TODAS as linhas ficaram velhas de uma vez, mas o p_dias=45 acharia
-- que estão frescas (foram calculadas há pouco) e não recalcularia nada. O dado
-- novo da Receita ficaria sem entrar até 45 dias depois.
--
-- Esta tabela guarda o run_key processado por último. Quando ele muda, o job
-- recalcula tudo (p_dias=0) em vez de confiar na janela de frescor.
--
-- Aplica: psql $POSTGRES_URL -f db/migrations/010_socio_job_controle.sql
-- Idempotente.

CREATE TABLE IF NOT EXISTS socio.job_controle (
    chave       TEXT PRIMARY KEY,
    valor       TEXT,
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE socio.job_controle IS
    'Estado do job socio_empresas entre execuções. Hoje guarda uma chave só: '
    'ultimo_run_key = run_key de cnpj.rf_socios na última execução completa. '
    'Mudou o run_key = carga nova da RF = recalcular tudo, ignorando p_dias.';
