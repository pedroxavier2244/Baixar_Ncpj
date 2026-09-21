"""
Health check — usado pelo CRM para saber se a API está operacional.

NÃO faz COUNT(*) em 40M linhas (lento).
Usa pg_matviews.ispopulated + reltuples (estimativa instantânea do PostgreSQL).
"""
import json

from fastapi import APIRouter, Request
from psycopg.rows import dict_row

from api.schemas import HealthResponse
from config import settings

router = APIRouter()

_V = settings.pg_serving_schema


@router.get("", response_model=HealthResponse)
async def health(request: Request):
    last_run_key: str | None = None
    try:
        data = json.loads(settings.status_file.read_text(encoding="utf-8"))
        last_run_key = data.get("last_run_key")
    except Exception:
        pass

    db_ok      = False
    cache_ok   = False
    mv_count: int | None = None

    # Verifica banco — usa reltuples (estimativa, instantâneo, sem scan)
    try:
        async with request.app.state.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("""
                    SELECT mv.ispopulated,
                           c.reltuples::bigint AS approx_rows
                    FROM   pg_matviews mv
                    JOIN   pg_class    c  ON c.relname = mv.matviewname
                    WHERE  mv.schemaname  = %s
                      AND  mv.matviewname = 'mv_cnpj_full'
                """, (_V,))
                row = await cur.fetchone()
                if row and row["ispopulated"]:
                    mv_count = row["approx_rows"]
                    db_ok = True
    except Exception:
        pass

    # Verifica Redis com ping simples
    try:
        await request.app.state.redis.ping()
        cache_ok = True
    except Exception:
        pass

    # Métricas de cache
    cache_hits: int | None = None
    cache_misses: int | None = None
    hit_rate: float | None = None
    try:
        hits_raw   = await request.app.state.redis.get("stats:cache_hits")
        misses_raw = await request.app.state.redis.get("stats:cache_misses")
        if hits_raw is not None or misses_raw is not None:
            hits   = int(hits_raw)   if hits_raw   else 0
            misses = int(misses_raw) if misses_raw else 0
            cache_hits   = hits
            cache_misses = misses
            total = hits + misses
            hit_rate = round(hits / total, 3) if total > 0 else None
    except Exception:
        pass

    return HealthResponse(
        status="ok" if db_ok else "degraded",
        last_success_run_key=last_run_key,
        mv_row_count=mv_count,
        db_ok=db_ok,
        cache_ok=cache_ok,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
        hit_rate=hit_rate,
    )
