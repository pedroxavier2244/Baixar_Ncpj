-- Migration 009: socio.calcular_lote() — o cálculo das empresas irmãs
--
-- Traduz para SQL a lógica que o motor fazia por HTTP em sense.py::enrich_socios:
--
--   QSA      -> sócios PF do CNPJ, pelo par (nome_socio, cnpj_cpf_socio)
--   reverso  -> todas as empresas onde aquele par aparece
--   irmãs    -> união dos reversos de todos os sócios, MENOS a própria empresa
--
-- O par (nome, CPF mascarado de 6 dígitos) é o mesmo identificador que o endpoint
-- /socios/buscar usa — ver api/routes/socios.py. Mantido idêntico de propósito: é
-- o que garante que a tabela bata com o que a API responde hoje.
--
-- Recebe os CNPJs do lote, calcula só o que falta ou envelheceu (p_dias), faz
-- upsert, e devolve a linha de TODOS os pedidos — inclusive os já frescos.
--
-- Idempotente: rodar duas vezes no mesmo lote não duplica nem corrompe.
--
-- ── DUAS DECISÕES QUE PARECEM ESTILO E NÃO SÃO ──────────────────────────────
--
-- 1) RETURNS SETOF socio.socio_empresas, e não RETURNS TABLE(...).
--    Com RETURNS TABLE, cada coluna declarada vira uma VARIÁVEL PL/pgSQL, e
--    qualquer referência não-qualificada a "cnpj" dentro do corpo passa a ser
--    ambígua — erro em tempo de EXECUÇÃO, não de criação: a função é criada com
--    sucesso e só quebra quando alguém chama. Foi o que aconteceu na primeira
--    versão (24/08/2026, "column reference cnpj is ambiguous"). SETOF de uma
--    tabela existente não cria variável nenhuma e o problema deixa de existir.
--
-- 2) CTEs, e não TEMP TABLE ... ON COMMIT DROP.
--    ON COMMIT DROP só limpa no COMMIT, então duas chamadas dentro da mesma
--    transação estouravam "relation already exists". A saída anterior era um
--    DROP TABLE no início da função — que, sem qualificar o schema, resolve pelo
--    search_path e poderia apagar uma tabela REAL homônima em cnpj. Com CTE não
--    há objeto criado, não há o que limpar, e a idempotência sai de graça.
--
-- Aplica: psql $POSTGRES_URL -f db/migrations/009_socio_empresas_calcular.sql
-- Re-aplicável: o DROP abaixo existe porque CREATE OR REPLACE recusa mudança de
-- tipo de retorno ("cannot change return type of existing function") — sem ele,
-- reaplicar esta migration sobre uma versão anterior falha.

DROP FUNCTION IF EXISTS socio.calcular_lote(TEXT[], INT, INT);

CREATE FUNCTION socio.calcular_lote(
    p_cnpjs     TEXT[],
    p_dias      INT DEFAULT 45,     -- recalcula linha mais velha que isto
    p_max_lista INT DEFAULT 100     -- teto de CNPJs no campo texto cnpjs_irmas
)
RETURNS SETOF socio.socio_empresas
LANGUAGE plpgsql
AS $$
BEGIN
    WITH pedido AS MATERIALIZED (
        -- Normaliza na entrada. CNPJ sem zero à esquerda é praga conhecida nesta
        -- casa: chega com 12/13 dígitos e casa com nada se não for preenchido.
        SELECT DISTINCT
               LPAD(REGEXP_REPLACE(c, '\D', '', 'g'), 14, '0')::CHAR(14) AS cnpj
        FROM UNNEST(p_cnpjs) AS c
        WHERE LENGTH(REGEXP_REPLACE(c, '\D', '', 'g')) BETWEEN 1 AND 14
    ),
    alvo AS MATERIALIZED (
        -- Só recalcula o que falta ou envelheceu.
        SELECT p.cnpj, LEFT(p.cnpj, 8)::CHAR(8) AS cnpj_basico
        FROM pedido p
        LEFT JOIN socio.socio_empresas se ON se.cnpj = p.cnpj
        WHERE se.cnpj IS NULL
           OR se.atualizado_em < NOW() - MAKE_INTERVAL(days => p_dias)
    ),
    qsa AS MATERIALIZED (
        -- Todos os sócios do alvo (PF e PJ — n_socios conta os dois).
        SELECT a.cnpj, a.cnpj_basico, s.nome_socio, s.cnpj_cpf_socio,
               s.identificador_socio, s.faixa_etaria
        FROM alvo a
        JOIN cnpj.rf_socios s ON s.cnpj_basico = a.cnpj_basico
    ),
    irmas AS (
        -- Reverso: empresas de cada sócio PF, casando pelo mesmo par que a API usa.
        -- identificador_socio = '2' é pessoa física; sócio PJ não gera "irmã de dono".
        -- Depende do índice idx_rf_socios_new_cpf_nome — sem ele isto vira seq scan
        -- sobre 28M linhas (3,1s por consulta, medido em 24/08/2026).
        SELECT DISTINCT
               q.cnpj,
               (e.cnpj_basico || e.cnpj_ordem || e.cnpj_dv)::CHAR(14) AS irma_cnpj
        FROM qsa q
        JOIN cnpj.rf_socios r
          ON r.cnpj_cpf_socio      = q.cnpj_cpf_socio
         AND r.nome_socio          = q.nome_socio
         AND r.identificador_socio = '2'
        JOIN cnpj.rf_estabelecimentos e
          ON e.cnpj_basico = r.cnpj_basico
         AND e.cnpj_ordem  = '0001'          -- matriz: sem isso, filial duplica
        WHERE q.identificador_socio = '2'
          AND q.cnpj_cpf_socio IS NOT NULL
          AND q.nome_socio     IS NOT NULL
          AND r.cnpj_basico <> q.cnpj_basico -- "irmã" exclui a própria empresa
    ),
    irmas_rank AS (
        SELECT i.cnpj, i.irma_cnpj,
               ROW_NUMBER() OVER (PARTITION BY i.cnpj ORDER BY i.irma_cnpj) AS rn,
               COUNT(*)     OVER (PARTITION BY i.cnpj)                      AS total
        FROM irmas i
    ),
    agg_irmas AS (
        -- n_empresas_dono guarda a contagem REAL; só o campo texto tem teto.
        -- Sócio de nome comum casa com dezenas de empresas e sem teto isso vira
        -- campo de vários KB.
        SELECT r.cnpj,
               MAX(r.total)::INT AS n_irmas,
               STRING_AGG(r.irma_cnpj, ';' ORDER BY r.irma_cnpj)
                   FILTER (WHERE r.rn <= p_max_lista) AS cnpjs_irmas
        FROM irmas_rank r
        GROUP BY r.cnpj
    ),
    agg_socios AS (
        -- faixa_etaria '0' = sócio PJ: não entra na média nem em n_socios_pf.
        -- Empresa só com PJ fica com média NULL, não zero.
        SELECT q.cnpj,
               COUNT(*)::INT AS n_socios,
               COUNT(*) FILTER (
                   WHERE q.faixa_etaria IS NOT NULL AND q.faixa_etaria <> '0'
               )::INT AS n_socios_pf,
               AVG(f.ponto_medio)::NUMERIC(5,2) AS faixa_etaria_media
        FROM qsa q
        LEFT JOIN socio.faixa_etaria_ponto_medio f ON f.codigo = q.faixa_etaria
        GROUP BY q.cnpj
    ),
    agg_na_base AS (
        -- irmas_na_base: quantas das irmas estao na NOSSA carteira.
        -- Cruza por cnpj_basico (8 digitos), NAO por CNPJ completo: as irmas sao
        -- sempre matriz (0001) e a carteira tem 205 filiais — casar completo
        -- devolveria zero nesses casos, e o erro seria invisivel.
        -- Conta sobre o conjunto INTEIRO de irmas, antes do teto do campo texto.
        SELECT i.cnpj, COUNT(DISTINCT LEFT(i.irma_cnpj, 8))::INT AS n_na_base
        FROM   irmas i
        JOIN   socio.base_cnpj b ON b.cnpj_basico = LEFT(i.irma_cnpj, 8)::CHAR(8)
        GROUP  BY i.cnpj
    ),
    calc AS MATERIALIZED (
        SELECT a.cnpj,
               EXISTS (SELECT 1 FROM cnpj.rf_estabelecimentos e
                        WHERE e.cnpj_basico = a.cnpj_basico) AS existe_rf,
               ai.n_irmas,
               ai.cnpjs_irmas,
               COALESCE(anb.n_na_base, 0) AS irmas_na_base,
               COALESCE(asoc.n_socios, 0)    AS n_socios,
               COALESCE(asoc.n_socios_pf, 0) AS n_socios_pf,
               asoc.faixa_etaria_media
        FROM alvo a
        LEFT JOIN agg_irmas   ai   ON ai.cnpj   = a.cnpj
        LEFT JOIN agg_socios  asoc ON asoc.cnpj = a.cnpj
        LEFT JOIN agg_na_base anb  ON anb.cnpj  = a.cnpj
    ),
    ins_log AS (
        -- CTE que escreve: é executada mesmo sem ninguém ler o resultado dela.
        INSERT INTO socio.socio_empresas_fetch_log (cnpj, buscado_em, status)
        SELECT c.cnpj, NOW(),
               CASE WHEN NOT c.existe_rf   THEN 'nao_encontrado'
                    WHEN c.n_socios_pf > 0 THEN 'ok'
                    ELSE 'sem_qsa'
               END
        FROM calc c
        ON CONFLICT (cnpj) DO UPDATE SET
            buscado_em = EXCLUDED.buscado_em,
            status     = EXCLUDED.status
        RETURNING 1
    )
    INSERT INTO socio.socio_empresas (
        cnpj, n_empresas_dono, irmas_na_base, cnpjs_irmas,
        n_socios, n_socios_pf, faixa_etaria_media, atualizado_em
    )
    SELECT c.cnpj,
           -- CNPJ ausente da RF fica NULL, não 1: "1 empresa" seria afirmar que a
           -- empresa existe. Quem separa os dois casos é o fetch_log.
           CASE WHEN c.existe_rf THEN COALESCE(c.n_irmas, 0) + 1 END,
           CASE WHEN c.existe_rf THEN c.irmas_na_base END,
           c.cnpjs_irmas,
           c.n_socios,
           c.n_socios_pf,
           c.faixa_etaria_media,
           NOW()
    FROM calc c
    ON CONFLICT (cnpj) DO UPDATE SET
        n_empresas_dono    = EXCLUDED.n_empresas_dono,
        irmas_na_base      = EXCLUDED.irmas_na_base,
        cnpjs_irmas        = EXCLUDED.cnpjs_irmas,
        n_socios           = EXCLUDED.n_socios,
        n_socios_pf        = EXCLUDED.n_socios_pf,
        faixa_etaria_media = EXCLUDED.faixa_etaria_media,
        atualizado_em      = EXCLUDED.atualizado_em;

    -- Devolve TODOS os pedidos: os recém-calculados e os que já estavam frescos.
    RETURN QUERY
    SELECT se.*
    FROM socio.socio_empresas se
    WHERE se.cnpj IN (
        SELECT LPAD(REGEXP_REPLACE(c, '\D', '', 'g'), 14, '0')::CHAR(14)
        FROM UNNEST(p_cnpjs) AS c
        WHERE LENGTH(REGEXP_REPLACE(c, '\D', '', 'g')) BETWEEN 1 AND 14
    )
    ORDER BY se.cnpj;
END;
$$;

COMMENT ON FUNCTION socio.calcular_lote IS
    'Calcula/atualiza socio.socio_empresas para um lote de CNPJs e devolve as '
    'linhas de todos os pedidos. Incremental (pula linha fresca), idempotente '
    '(upsert). Chamada pelo job noturno e pelo endpoint de lote da API.';
