@echo off
setlocal

:: ── Config ────────────────────────────────────────────────────────────────
set CONTAINER=cnpj_e2e_test
set PG_PORT=5433
set PG_PASS=test
set POSTGRES_URL=postgresql://postgres:%PG_PASS%@localhost:%PG_PORT%/postgres

set RESULT=0

:: ── Remove container anterior se existir ──────────────────────────────────
docker rm -f %CONTAINER% >nul 2>&1

:: ── Start PostgreSQL ───────────────────────────────────────────────────────
echo [e2e] Subindo PostgreSQL...
docker run -d --name %CONTAINER% ^
    -p %PG_PORT%:5432 ^
    -e POSTGRES_PASSWORD=%PG_PASS% ^
    postgres:15-alpine >nul 2>&1

if %ERRORLEVEL% neq 0 (
    echo [e2e] ERRO: falha ao iniciar container Docker.
    echo [e2e] Docker esta rodando? docker ps
    exit /b 1
)

:: ── Aguardar PostgreSQL estar pronto ──────────────────────────────────────
echo [e2e] Aguardando PostgreSQL...
set attempts=0
:wait_pg
docker exec %CONTAINER% pg_isready -U postgres >nul 2>&1
if %ERRORLEVEL% equ 0 goto pg_ready
set /a attempts+=1
if %attempts% geq 30 (
    echo [e2e] ERRO: PostgreSQL nao iniciou em 30s.
    set RESULT=1
    goto cleanup
)
timeout /t 1 /nobreak >nul
goto wait_pg

:pg_ready
echo [e2e] PostgreSQL pronto.

:: ── Aplicar schema ────────────────────────────────────────────────────────
echo [e2e] Aplicando schema.sql...
docker cp db\schema.sql %CONTAINER%:/tmp/schema.sql >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [e2e] ERRO: falha ao copiar schema.sql para o container.
    echo [e2e] O arquivo db\schema.sql existe?
    set RESULT=1
    goto cleanup
)
docker exec %CONTAINER% psql -U postgres -f /tmp/schema.sql >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [e2e] ERRO: falha ao aplicar schema.sql
    set RESULT=1
    goto cleanup
)
echo [e2e] Schema aplicado.

:: ── Rodar testes ──────────────────────────────────────────────────────────
echo [e2e] Executando e2e_test.py...
python tests\e2e_test.py
set RESULT=%ERRORLEVEL%

:: ── Cleanup ───────────────────────────────────────────────────────────────
:cleanup
echo [e2e] Removendo container...
docker stop %CONTAINER% >nul 2>&1
docker rm   %CONTAINER% >nul 2>&1

echo.
if %RESULT% equ 0 (
    echo [e2e] ============================================
    echo [e2e]  TODOS OS TESTES PASSARAM
    echo [e2e] ============================================
) else (
    echo [e2e] ============================================
    echo [e2e]  TESTES FALHARAM
    echo [e2e] ============================================
)
exit /b %RESULT%
