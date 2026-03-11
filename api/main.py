"""
FastAPI app — async psycopg3 pool, Redis cache, rate limiting, API Key auth.

Start (Linux/macOS — production):
    gunicorn api.main:app -w 8 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000

Start (Windows — development):
    uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers 4
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import psycopg_pool
import redis.asyncio as aioredis
from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from api.limiter import limiter
from api.routes import cnpj as cnpj_router
from api.routes import health as health_router
from api.routes import runs as runs_router
from api.routes import search as search_router
from config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Banco de dados — pool com timeout para não enfileirar forever
    pool = psycopg_pool.AsyncConnectionPool(
        conninfo=settings.postgres_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        timeout=settings.db_pool_timeout,
        open=False,
    )
    await pool.open()
    app.state.pool = pool

    # Cache Redis — com degradação graceful se não estiver disponível
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    app.state.redis = redis

    try:
        yield
    finally:
        await pool.close()
        try:
            await redis.aclose()
        except Exception:
            pass


app = FastAPI(
    title="ETL CNPJ — API de Consulta",
    description="Consulta de CNPJs com dados da Receita Federal do Brasil",
    version="1.0.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Health e runs não exigem API Key — o CRM monitora sem autenticação
app.include_router(health_router.router, prefix="/health", tags=["Health"])
app.include_router(runs_router.router,   prefix="/runs",   tags=["Runs"])

# Rotas principais — API Key obrigatória (configurada em cada router)
app.include_router(cnpj_router.router,    prefix="/cnpj",    tags=["CNPJ"])
app.include_router(search_router.router,  prefix="/search",  tags=["Busca"])
