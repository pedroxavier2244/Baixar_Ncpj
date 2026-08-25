-- Migration 008: socio_empresas — empresas irmãs (mesmo dono) pré-computadas
--
-- Contexto: a relação sócio -> outras empresas do mesmo dono era resolvida no
-- motor (sense.py::enrich_socios) batendo em /cnpj/{cnpj}/socios e
-- /socios/buscar por CNPJ, gravando o CSV 29-socio-empresas.csv. Cobertura ~27%
-- e envelhecendo, porque só as linhas da foto do dia eram enriquecidas.
--
-- Aqui isso vira tabela. Como rf_socios já mora neste banco, o cálculo é SQL
-- direto — nenhuma chamada HTTP sai daqui. Efeito colateral bem-vindo: some a
-- dependência de /socios/buscar, que só existe no IP em HTTP puro (o espelho
-- HTTPS responde 404 nele) e por isso era inalcançável pelo front.
--
-- POR QUE TABELA E NÃO COLUNA: o load_step reconstrói rf_empresas,
-- rf_estabelecimentos, rf_socios e rf_simples do zero a cada carga mensal
-- (monta a _new a partir dos CSVs da RF e renomeia por cima). Coluna nossa
-- nessas tabelas é apagada na virada do mês sem aviso. Schema próprio, que o
-- swap não toca. Também não usar public — regra do banco corporativo.
--
-- ESCOPO: aqui mora só o que a Receita sabe. "Quantas irmãs estão na nossa
-- carteira" é pergunta do CRM e é respondida lá, contra o data_base.
--
-- Aplica: psql $POSTGRES_URL -f db/migrations/008_socio_empresas.sql
-- Idempotente: pode rodar duas vezes.

CREATE SCHEMA IF NOT EXISTS socio;

COMMENT ON SCHEMA socio IS
    'Derivados de vínculo societário servidos pela API. Fonte: cnpj.rf_socios. '
    'Não participa do swap mensal do ETL.';


-- ── A tabela pedida na issue (recorte VPS) ──────────────────────────────────
-- Preenchida sob demanda pelo endpoint de lote: o CRM manda os CNPJs que quer,
-- a função calcula o que estiver velho ou faltando e devolve. Serve de cache
-- persistente — não recria, faz upsert.

CREATE TABLE IF NOT EXISTS socio.socio_empresas (
    cnpj                CHAR(14) PRIMARY KEY,
    n_empresas_dono     INT,        -- empresas do(s) dono(s), INCLUI a própria
    cnpjs_irmas         TEXT,       -- CNPJs irmãos, ;-joined (matriz, ordem 0001)
    n_socios            INT,
    n_socios_pf         INT,        -- sócios pessoa física (faixa_etaria <> '0')
    faixa_etaria_media  NUMERIC,    -- média dos pontos-médios das faixas dos PF
    atualizado_em       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE socio.socio_empresas IS
    'Empresas irmãs por CNPJ, derivadas de cnpj.rf_socios. '
    'irmas_na_base NÃO mora aqui: é contado no CRM contra o data_base.';

COMMENT ON COLUMN socio.socio_empresas.faixa_etaria_media IS
    'NULL (não zero) quando a empresa só tem sócio PJ — faixa_etaria 0 é ignorada.';

CREATE INDEX IF NOT EXISTS idx_socio_empresas_atualizado
    ON socio.socio_empresas (atualizado_em);


-- ── Controle: progresso e re-frescor ────────────────────────────────────────
-- É o que responde "até onde o job chegou" e "quão velha está esta linha".

CREATE TABLE IF NOT EXISTS socio.socio_empresas_fetch_log (
    cnpj        CHAR(14) PRIMARY KEY,
    buscado_em  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status      TEXT NOT NULL
        CHECK (status IN ('ok', 'sem_qsa', 'nao_encontrado'))
);

CREATE INDEX IF NOT EXISTS idx_fetch_log_buscado_em
    ON socio.socio_empresas_fetch_log (buscado_em);

COMMENT ON COLUMN socio.socio_empresas_fetch_log.status IS
    'ok = calculado com sócio PF | sem_qsa = existe na RF mas sem sócio PF '
    '(só PJ, ou QSA vazio) | nao_encontrado = CNPJ ausente de rf_estabelecimentos';


-- ── Decodificação da faixa etária (código RF -> ponto médio) ────────────────
-- O campo faixa_etaria do QSA é código, não idade. 0 = sócio PJ, ignorar.

CREATE TABLE IF NOT EXISTS socio.faixa_etaria_ponto_medio (
    codigo       CHAR(1) PRIMARY KEY,
    descricao    TEXT NOT NULL,
    ponto_medio  INT
);

INSERT INTO socio.faixa_etaria_ponto_medio (codigo, descricao, ponto_medio) VALUES
    ('1', '0 a 12 anos',   6),
    ('2', '13 a 20 anos', 16),
    ('3', '21 a 30 anos', 25),
    ('4', '31 a 40 anos', 35),
    ('5', '41 a 50 anos', 45),
    ('6', '51 a 60 anos', 55),
    ('7', '61 a 70 anos', 65),
    ('8', '71 a 80 anos', 75),
    ('9', 'maior de 80 anos', 85),
    ('0', 'sócio pessoa jurídica', NULL)
ON CONFLICT (codigo) DO UPDATE
    SET descricao   = EXCLUDED.descricao,
        ponto_medio = EXCLUDED.ponto_medio;


-- ── Índice de apoio ao reverso ──────────────────────────────────────────────
-- O reverso casa (cnpj_cpf_socio, nome_socio) contra rf_socios inteira, e o
-- indice que sustenta isso e o idx_rf_socios_new_cpf_nome. Ele e criado pelo
-- index_sqls do steps/load_step.py (e, entre cargas, pela migration 006). O
-- calculo reusa esse indice — nada novo e necessario aqui.
--
-- SE ELE SUMIR, este calculo vira seq scan sobre 28M linhas. Ja aconteceu: em
-- 24/08/2026 o indice foi encontrado ausente porque so existia na migration 006,
-- aplicada a mao, e o swap mensal do load_step o apagava. Conferir com:
--   SELECT indexname FROM pg_indexes
--    WHERE schemaname='cnpj' AND tablename='rf_socios';
