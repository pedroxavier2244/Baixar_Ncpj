# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Instala dependências de sistema mínimas para compilar psycopg e outros
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Copia dependências já instaladas do build stage
COPY --from=builder /install /usr/local

# Copia apenas o código necessário (sem venv local, sem dados, sem logs)
COPY api/        ./api/
COPY steps/      ./steps/
COPY db/         ./db/
COPY config.py   logger.py orchestrator.py worker.py \
     enqueue_job.py scheduler.py setup.py ./

# Diretórios de runtime — serão montados como volumes no docker-compose
RUN mkdir -p /data /logs /checkpoints

# Usuário não-root por segurança
RUN useradd -m -u 1001 appuser && chown -R appuser /app /data /logs /checkpoints
USER appuser

# Porta da API
EXPOSE 8000

# Health check — o Docker verifica se a API está respondendo a cada 30s
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# Comando padrão: API em produção com gunicorn (8 workers)
CMD ["gunicorn", "api.main:app", \
     "-w", "8", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "-b", "0.0.0.0:8000", \
     "--timeout", "30", \
     "--keep-alive", "5", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
