"""
FastAPI app — async psycopg3 connection pool, Gunicorn-compatible.

Start (Linux/macOS — production):
    gunicorn api.main:app -w 8 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000

Start (Windows — development):
    uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers 4
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import psycopg_pool
from fastapi import FastAPI

from api.routes import cnpj as cnpj_router
from api.routes import health as health_router
from api.routes import runs as runs_router
from api.routes import search as search_router
from config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = psycopg_pool.AsyncConnectionPool(
        conninfo=settings.postgres_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        open=False,
    )
    await pool.open()
    app.state.pool = pool
    try:
        yield
    finally:
        await pool.close()


app = FastAPI(
    title="ETL CNPJ — API de Consulta",
    description="Consulta de CNPJs com dados da Receita Federal do Brasil",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(cnpj_router.router, prefix="/cnpj", tags=["CNPJ"])
app.include_router(search_router.router, prefix="/search", tags=["Busca"])
app.include_router(health_router.router, prefix="/health", tags=["Health"])
app.include_router(runs_router.router, prefix="/runs", tags=["Runs"])
