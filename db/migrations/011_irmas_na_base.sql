-- Migration 011: irmas_na_base — quantas irmãs estão na NOSSA carteira
--
-- Coluna que a issue pedia desde o começo e que tinha ficado de fora: ela depende
-- do data_base, que é do CRM, e este banco não conhecia a carteira. A decisão de
-- 24/08/2026 foi "o CRM calcula"; em 25/08/2026 mudou — o motor precisa do mesmo
-- número em paralelo ao CRM, então ele passa a sair daqui.
--
-- O insumo já chegava de graça: o job lê a carteira inteira toda noite para saber
-- QUAIS CNPJs processar. Guardá-la numa tabela transforma "está na carteira?" num
-- JOIN, e o número sai junto do resto do cálculo.
--
-- ⚠️  O CRUZAMENTO É POR cnpj_basico (8 dígitos), NÃO POR CNPJ COMPLETO.
-- As irmãs são sempre matriz (cnpj_ordem = '0001'), mas a carteira tem 205 filiais
-- (158 em 0002, 24 em 0003, 13 em 0004, e outras — medido em 25/08/2026). Cruzar
-- CNPJ completo devolveria zero justamente nesses casos, e o erro seria invisível:
-- um número plausível, só que menor. Filial e matriz são a mesma empresa.
--
-- Aplica: psql $POSTGRES_URL -f db/migrations/011_irmas_na_base.sql
-- Idempotente.

-- ── A carteira, espelhada aqui ──────────────────────────────────────────────
-- Só CNPJ. Nenhum dado de cliente atravessa: quem responde "está na carteira?"
-- não precisa saber de quem é.
CREATE TABLE IF NOT EXISTS socio.base_cnpj (
    cnpj          CHAR(14) PRIMARY KEY,
    cnpj_basico   CHAR(8) NOT NULL,
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_base_cnpj_basico
    ON socio.base_cnpj (cnpj_basico);

COMMENT ON TABLE socio.base_cnpj IS
    'Espelho dos CNPJs da carteira (data_base do Supabase do CRM), sincronizado '
    'pelo job a cada execução. Existe só para o JOIN de irmas_na_base — nenhum '
    'dado de cliente vem junto.';

COMMENT ON COLUMN socio.base_cnpj.cnpj_basico IS
    'Os 8 primeiros dígitos. É por AQUI que se cruza com as irmãs: elas são '
    'matriz (0001) e a carteira tem filiais, então casar CNPJ completo perderia '
    'os casos de filial em silêncio.';


ALTER TABLE socio.socio_empresas
    ADD COLUMN IF NOT EXISTS irmas_na_base INT;

COMMENT ON COLUMN socio.socio_empresas.irmas_na_base IS
    'Quantas das empresas irmãs estão na nossa carteira (socio.base_cnpj), '
    'cruzando por cnpj_basico. NÃO inclui a própria empresa — irmã exclui ela. '
    'NULL quando o CNPJ não foi achado na Receita.';


-- ── Recontagem barata, sem refazer o cálculo pesado ─────────────────────────
-- Por que existe: irmas_na_base envelhece por um motivo diferente do resto da
-- linha. Os dados da Receita mudam uma vez por mês, mas a CARTEIRA muda todo dia
-- — e quando um lead novo entra, ele vira "irmã na base" de linhas que já estão
-- gravadas e que o p_dias considera frescas. Sem esta recontagem, o número só se
-- corrigiria até 45 dias depois.
--
-- Ela reconta a partir do cnpjs_irmas já gravado, sem tocar em rf_socios: é um
-- JOIN de 22k contra 22k, questão de milissegundos.
--
-- LIMITE HONESTO: cnpjs_irmas tem teto (p_max_lista, hoje 100). Se alguma empresa
-- passar disso, a recontagem subestima — por isso a função DEVOLVE quantas linhas
-- estão nessa situação, para o job avisar em vez de mentir baixinho. Em
-- 25/08/2026 o máximo real era 40 irmãs, então o teto não morde.
CREATE OR REPLACE FUNCTION socio.recontar_irmas_na_base(p_max_lista INT DEFAULT 100)
RETURNS TABLE (linhas_atualizadas INT, linhas_no_teto INT)
LANGUAGE plpgsql
AS $$
DECLARE
    v_atualizadas INT;
    v_no_teto     INT;
BEGIN
    WITH irma AS (
        SELECT se.cnpj,
               LEFT(TRIM(i), 8)::CHAR(8) AS irma_basico
        FROM   socio.socio_empresas se,
               LATERAL UNNEST(string_to_array(se.cnpjs_irmas, ';')) AS i
        WHERE  se.cnpjs_irmas IS NOT NULL
    ),
    conta AS (
        SELECT ir.cnpj, COUNT(DISTINCT ir.irma_basico)::INT AS n
        FROM   irma ir
        JOIN   socio.base_cnpj b ON b.cnpj_basico = ir.irma_basico
        GROUP  BY ir.cnpj
    )
    UPDATE socio.socio_empresas se
       SET irmas_na_base = COALESCE(c.n, 0)
      FROM (SELECT cnpj FROM socio.socio_empresas) todos
      LEFT JOIN conta c ON c.cnpj = todos.cnpj
     WHERE se.cnpj = todos.cnpj
       AND se.n_empresas_dono IS NOT NULL            -- achado na Receita
       AND se.irmas_na_base IS DISTINCT FROM COALESCE(c.n, 0);
    GET DIAGNOSTICS v_atualizadas = ROW_COUNT;

    SELECT count(*)::INT INTO v_no_teto
    FROM   socio.socio_empresas
    WHERE  n_empresas_dono - 1 > p_max_lista;

    RETURN QUERY SELECT v_atualizadas, v_no_teto;
END;
$$;

COMMENT ON FUNCTION socio.recontar_irmas_na_base IS
    'Reconta irmas_na_base para a tabela toda a partir do cnpjs_irmas gravado, '
    'sem tocar em rf_socios. Devolve (linhas atualizadas, linhas cujo cnpjs_irmas '
    'foi cortado pelo teto e por isso subestimam).';
