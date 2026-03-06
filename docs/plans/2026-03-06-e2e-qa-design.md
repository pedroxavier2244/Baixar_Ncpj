# E2E QA Test — Design

**Goal:** Teste ponta a ponta completo do pipeline ETL CNPJ + API HTTP, sem depender da Receita Federal real, usando PostgreSQL efêmero via Docker.

**Architecture:** Script BAT Windows orquestra um container PostgreSQL Docker (porta 5433), aplica o schema, executa um script Python QA que cria fixtures no formato real da RF, roda todos os 6 steps do pipeline (verify → extract → transform → load → index), valida os dados via SQL direto e depois sobe a API FastAPI com uvicorn para testar os endpoints HTTP.

**Tech Stack:** Python 3.13+, psycopg 3, FastAPI, httpx, uvicorn, Docker (postgres:15-alpine), Windows BAT

---

## Componentes

### `run_e2e.bat`
Orchestrador Windows:
1. `docker run -d --name cnpj_e2e_test -p 5433:5432 -e POSTGRES_PASSWORD=test postgres:15-alpine`
2. Loop de espera `pg_isready` (até 30 tentativas, 1s cada)
3. `psql -f db/schema.sql` no container
4. `python tests/e2e_test.py`
5. `docker stop cnpj_e2e_test && docker rm cnpj_e2e_test` (sempre, mesmo em erro)
6. Exit code propagado do Python

### `tests/e2e_test.py`
Script QA Python standalone (sem pytest):

**FASE 1 — Fixtures**
- Gera CSVs no formato real da RF: latin-1, separador `;`, sem header, colunas em posição fixa
- Dados: 3 empresas, 3 estabelecimentos (1 por empresa), 2 socios, 3 simples, lookups completos
- CNPJ de referência para assertions: `11111111` (basico), `111111110001XX` (completo)
- Empacota cada CSV em ZIP com nome interno no formato real da RF:
  - `K3241.K03200Y0.D51213.EMPRECSV` dentro de `Empresas0.zip`
  - `K3241.K03200Y0.D51213.ESTABELE` dentro de `Estabelecimentos0.zip`
  - `K3241.K03200Y0.D51213.SOCIOCSV` dentro de `Socios0.zip`
  - `F.K03200$W.SIMPLES.CSV.D51213` dentro de `Simples.zip`
  - Lookups: `F.K03200$Z.D51213.CNAECSV`, `F.K03200$Z.D51213.MUNICCSV`, etc.

**FASE 2 — Pipeline ETL**
- Injeta `download_manifest.json` apontando para os ZIPs locais (bypassa download real)
- Roda steps em ordem com assertions de StepResult.status == SUCCESS:
  - `verify` — valida ZIPs criados pelas fixtures
  - `extract` — extrai CSVs dos ZIPs
  - `transform` — normaliza encoding + adiciona headers
  - `load` — COPY staging + diff_merge + replace para main tables
  - `index` — REFRESH MATERIALIZED VIEW cnpj_serving.mv_cnpj_full

**FASE 3 — Validações SQL**
- `cnpj.rf_empresas` tem 3 linhas
- `cnpj.rf_estabelecimentos` tem 3 linhas
- `cnpj.rf_socios` tem 2 linhas
- `cnpj.rf_simples` tem 3 linhas
- `cnpj_serving.mv_cnpj_full` tem 3 linhas
- Empresa `11111111` tem razao_social = `EMPRESA TESTE LTDA`
- Empresa `22222222` com opcao_pelo_simples = `S`

**FASE 4 — API HTTP**
- Sobe `uvicorn api.main:app --port 8765` em subprocess com POSTGRES_URL apontando para Docker
- Aguarda `/health` responder (até 10 tentativas, 1s cada)
- `GET /health` → `status=ok`, `mv_row_count=3`
- `GET /cnpj/11111111000141` → `razao_social=EMPRESA TESTE LTDA`
- `GET /cnpj/11111111` → retorna dados (busca por basico)
- `GET /search?razao_social=TESTE` → lista com ≥1 resultado
- `GET /search?uf=SP` → lista com ≥1 resultado
- Para uvicorn subprocess

**RELATÓRIO**
- Conta assertions pass/fail
- Imprime resumo no final
- Exit code 0 = tudo passou, 1 = algum falhou

---

## Dados de Fixture

### cnpj_basico de referência

| cnpj_basico | razao_social          | porte | simples |
|---|---|---|---|
| 11111111    | EMPRESA TESTE LTDA    | 03    | N       |
| 22222222    | COMERCIO TESTE SA     | 05    | S       |
| 33333333    | SERVICOS BRASIL EIRELI| 01    | N       |

CNPJ completo da empresa 1: `11111111` + `0001` + `41` = `11111111000141`

---

## Isolamento

- Container Docker destruído ao final (sempre)
- POSTGRES_URL aponta para porta 5433 (não conflita com prod)
- Diretório temporário para fixtures e checkpoints: `tests/e2e_tmp/`
- Limpeza automática ao final

---

## Critérios de Sucesso

- Todos os 6 steps do pipeline retornam SUCCESS
- Dados das fixtures estão presentes no PostgreSQL com contagens corretas
- MV populada e retornando dados corretos
- API responde corretamente em todos os 5 endpoints testados
