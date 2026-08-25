-- ============================================================================
-- EVIDÊNCIA — socio_empresas (issue "Job noturno: sócio->empresas vira tabela")
--
-- Roda os critérios de aceite da issue e imprime o resultado de cada um, na
-- ordem em que a issue os lista. Saída pronta para colar no ticket.
--
-- É SÓ LEITURA, com uma exceção declarada: o teste de idempotência (bloco 5)
-- chama socio.calcular_lote() duas vezes para provar que a segunda não altera
-- nada. Ele grava — mas só nos CNPJs de amostra, e é justamente o que a issue
-- manda demonstrar.
--
-- Rodar:
--   ssh mbfinance "docker exec -i <container-pg> psql -U etl_user -d cnpj_db" \
--       < db/evidencia_socio_empresas.sql
-- ============================================================================

\pset border 2
\timing on

\echo ''
\echo '################ EVIDÊNCIA socio_empresas ################'
\echo ''

-- ── 0. Pré-requisito: o índice do reverso existe? ───────────────────────────
-- Não está nos critérios da issue, mas está antes deles: sem este índice o
-- cálculo vira seq scan sobre 28M linhas e nada abaixo se sustenta. Foi
-- encontrado AUSENTE em 24/08/2026 — o swap mensal do ETL o apagava.
\echo '=== 0. PRÉ-REQUISITO — índice do reverso ==='
SELECT i.indexname,
       pg_size_pretty(pg_relation_size(c.oid))         AS tamanho,
       CASE WHEN x.indisvalid THEN 'válido'
            ELSE 'INVÁLIDO — precisa recriar' END      AS estado
FROM   pg_indexes i
JOIN   pg_class   c ON c.relname = i.indexname
JOIN   pg_index   x ON x.indexrelid = c.oid
WHERE  i.schemaname = 'cnpj'
  AND  i.tablename  = 'rf_socios'
ORDER  BY i.indexname;

\echo ''
\echo '--- o reverso usa o índice? (esperado: Index Scan, NÃO Seq Scan) ---'
EXPLAIN SELECT cnpj_basico FROM cnpj.rf_socios
 WHERE cnpj_cpf_socio = '***352681**' AND nome_socio = 'ADEMIR SILGUEIRO';

\echo ''
\echo '=== 1. ACEITE — a tabela existe e é populada por upsert ==='
SELECT to_char(count(*), 'FM999,999,999')                         AS linhas,
       to_char(count(*) FILTER (WHERE n_empresas_dono IS NOT NULL),
               'FM999,999,999')                                   AS com_dado_rf,
       pg_size_pretty(pg_total_relation_size('socio.socio_empresas')) AS tamanho
FROM   socio.socio_empresas;

\echo ''
\echo '--- upsert, não recria: PK impede duplicata (esperado: 0) ---'
SELECT count(*) AS cnpjs_duplicados
FROM  (SELECT cnpj FROM socio.socio_empresas
       GROUP BY cnpj HAVING count(*) > 1) d;

\echo ''
\echo '=== 2. ACEITE — cobertura ==='
-- Cobertura de PROCESSAMENTO: todo CNPJ pedido tem linha. É este o número que a
-- issue compara com os ~27% do CSV — lá, só as linhas da foto do dia eram
-- enriquecidas; aqui a base inteira é varrida.
SELECT to_char(count(*), 'FM999,999,999')                       AS cnpjs_processados,
       to_char(count(*) FILTER (WHERE status <> 'nao_encontrado'),
               'FM999,999,999')                                 AS achados_na_rf,
       to_char(count(*) FILTER (WHERE status = 'ok'),
               'FM999,999,999')                                 AS com_socio_pf
FROM   socio.socio_empresas_fetch_log;

\echo ''
\echo '--- por que "com_socio_pf" NÃO é a cobertura (e não é um buraco) ---'
-- ATENÇÃO À LEITURA: a maioria do que fica sem sócio é Empresário Individual, e
-- a Receita NÃO publica quadro societário de EI — não existe QSA para buscar.
-- Contar isso como falta transforma um limite do dado em bug inexistente. A
-- régua honesta é: dos CNPJs que PODEM ter QSA, quantos têm.
WITH c AS (
    SELECT l.status,
           (n.descricao ILIKE '%Empres_rio%Individual%') AS eh_ei
    FROM   socio.socio_empresas_fetch_log l
    -- ::CHAR(8) NAO e enfeite: sem ele, LEFT() devolve text, o Postgres casta
    -- cnpj_basico para text do lado da tabela GRANDE, o indice deixa de servir e
    -- o plano vira Merge Join ordenando as 69M linhas de rf_empresas — 5min10s
    -- medidos. Com o cast, e index lookup dos 22k. (medido em 24/08/2026)
    LEFT   JOIN cnpj.rf_empresas   e ON e.cnpj_basico = LEFT(l.cnpj, 8)::CHAR(8)
    LEFT   JOIN cnpj.rf_naturezas  n ON n.codigo      = e.natureza_juridica
)
SELECT to_char(count(*) FILTER (WHERE status = 'nao_encontrado'),
               'FM999,999')                                     AS fora_da_rf,
       to_char(count(*) FILTER (WHERE eh_ei), 'FM999,999')      AS empresario_individual,
       to_char(count(*) FILTER (WHERE status = 'ok'), 'FM999,999') AS com_qsa,
       round(100.0 * count(*) FILTER (WHERE status = 'ok')
             / NULLIF(count(*) FILTER (WHERE status <> 'nao_encontrado'
                                         AND NOT COALESCE(eh_ei, false)), 0), 2) || '%'
                                                                AS pct_dos_que_podem
FROM   c;

\echo ''
\echo '--- normalização de CNPJ sem zero à esquerda funcionou? ---'
-- A data_base entrega ~1.081 CNPJs com 11/12/13 dígitos (praga conhecida). Se a
-- normalização falhasse, eles cairiam todos em "nao_encontrado".
SELECT l.status,
       count(*) FILTER (WHERE l.cnpj LIKE '0%') AS vieram_sem_zero,
       count(*)                                 AS total
FROM   socio.socio_empresas_fetch_log l
GROUP  BY l.status ORDER BY total DESC;
\echo '--- LEITURA: se "nao_encontrado" tem 0 na coluna do meio, a normalização ---'
\echo '--- funcionou: nenhum CNPJ se perdeu por falta de zero à esquerda.      ---'

\echo ''
\echo '=== 3. ACEITE — faixa_etaria_media vazia (não zero) onde só há PJ ==='
-- A issue pede exatamente isto: preenchida onde há sócio PF, VAZIA onde não há.
-- "vazia" tem que ser NULL; zero seria afirmar idade média zero.
SELECT CASE WHEN n_socios_pf > 0 THEN 'com sócio PF'
            ELSE 'sem sócio PF (só PJ / sem QSA)' END            AS grupo,
       count(*)                                                  AS linhas,
       count(faixa_etaria_media)                                 AS com_media,
       count(*) FILTER (WHERE faixa_etaria_media = 0)            AS media_zero_BUG,
       round(min(faixa_etaria_media), 1)                         AS media_min,
       round(max(faixa_etaria_media), 1)                         AS media_max
FROM   socio.socio_empresas
GROUP  BY 1 ORDER BY 1;

\echo ''
\echo '--- LEITURA: em "sem sócio PF", com_media e media_zero_BUG devem ser 0 ---'

\echo ''
\echo '=== 4. ACEITE — fetch_log mostra progresso e re-frescor ==='
SELECT status,
       count(*)                                        AS cnpjs,
       min(buscado_em)::timestamp(0)                   AS mais_antigo,
       max(buscado_em)::timestamp(0)                   AS mais_recente,
       max(age(now(), buscado_em))::interval(0)        AS idade_maxima
FROM   socio.socio_empresas_fetch_log
GROUP  BY status ORDER BY cnpjs DESC;

\echo ''
\echo '--- quantos venceriam a janela de 45 dias hoje ---'
SELECT count(*) FILTER (WHERE buscado_em < now() - interval '45 days') AS a_revisitar,
       count(*)                                                        AS total
FROM   socio.socio_empresas_fetch_log;

\echo ''
\echo '=== 5. ACEITE — job idempotente (rodar 2x não duplica nem corrompe) ==='
-- Prova em três tempos sobre os mesmos CNPJs: estado, 2ª chamada, estado de novo.
-- Se a função fosse destrutiva ou não-incremental, atualizado_em mudaria.
-- SEM "ON COMMIT DROP": o psql roda cada comando em sua própria transação, então
-- a tabela seria destruída no commit do próprio CREATE e o SELECT abaixo falharia
-- com "relation does not exist". Temp table comum vive até o fim da sessão.
DROP TABLE IF EXISTS _antes, _depois;

CREATE TEMP TABLE _antes AS
SELECT * FROM socio.calcular_lote(ARRAY[
    '49005442000381',   -- A SUDESTE CLIMATIZACAO — 3 empresas (conferido na tela)
    '22059709000103'    -- ADEMIR SILGUEIRO — empresário individual, sem QSA
]);

CREATE TEMP TABLE _depois AS
SELECT * FROM socio.calcular_lote(ARRAY[
    '49005442000381',
    '22059709000103'
]);

SELECT a.cnpj,
       a.n_empresas_dono,
       a.n_socios,
       CASE WHEN a.atualizado_em = d.atualizado_em
            THEN 'inalterado — pulou (linha fresca)'
            ELSE 'RECALCULOU — verificar p_dias' END AS segunda_chamada,
       CASE WHEN a IS NOT DISTINCT FROM d
            THEN 'idêntico'
            ELSE 'DIVERGIU' END                      AS resultado
FROM   _antes a JOIN _depois d USING (cnpj)
ORDER  BY a.cnpj;

DROP TABLE IF EXISTS _antes, _depois;

\echo ''
\echo '--- LEITURA: as duas colunas devem dizer "inalterado" e "idêntico" ---'

\echo ''
\echo '=== 6. ACEITE — regressão contra o enrich_socios (amostra) ==='
-- A issue pede 20 CNPJs conferidos contra o 29-socio-empresas.csv. O CSV e o
-- sense.py não estão nesta máquina: esta consulta GERA a amostra para quem tem
-- o motor comparar. Não é o critério cumprido — é o insumo dele.
SELECT se.cnpj,
       se.n_empresas_dono,
       se.n_socios,
       se.n_socios_pf,
       se.faixa_etaria_media,
       left(coalesce(se.cnpjs_irmas, ''), 60) AS cnpjs_irmas_60c
FROM   socio.socio_empresas se
JOIN   socio.socio_empresas_fetch_log l ON l.cnpj = se.cnpj AND l.status = 'ok'
ORDER  BY se.cnpj
LIMIT  20;

\echo ''
\echo '=== 7. NÃO ENTREGUE NESTA TABELA — irmas_na_base ==='
\echo 'A issue lista a coluna irmas_na_base (quantas irmãs estão na NOSSA'
\echo 'carteira). Ela não mora aqui: depende do data_base, que é do CRM, e este'
\echo 'banco não conhece a carteira. Decisão do PO em 24/08/2026: o CRM calcula,'
\echo 'cruzando cnpjs_irmas (acima) contra o data_base. Esta tabela entrega o'
\echo 'insumo; a contagem é do outro lado.'

\echo ''
\echo '################ FIM ################'
