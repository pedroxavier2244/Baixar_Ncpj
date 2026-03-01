import json

from fastapi import APIRouter, Request
from psycopg.rows import dict_row

from api.schemas import HealthResponse

router = APIRouter()


@router.get("", response_model=HealthResponse)
async def health(request: Request):
    from config import settings

    last_run_key: str | None = None
    try:
        data = json.loads(settings.status_file.read_text(encoding="utf-8"))
        last_run_key = data.get("last_run_key")
    except Exception:
        pass

    mv_count: int | None = None
    try:
        async with request.app.state.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT COUNT(*) AS c FROM cnpj.mv_cnpj_full")
                row = await cur.fetchone()
                mv_count = row["c"] if row else None
    except Exception:
        pass

    return HealthResponse(
        status="ok",
        last_success_run_key=last_run_key,
        mv_row_count=mv_count,
    )
