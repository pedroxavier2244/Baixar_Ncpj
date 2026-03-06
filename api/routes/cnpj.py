from fastapi import APIRouter, HTTPException, Request
from psycopg.rows import dict_row

from api.schemas import CNPJResponse
from config import settings

router = APIRouter()

_MV = f"{settings.pg_serving_schema}.mv_cnpj_full"

_SELECT = f"SELECT * FROM {_MV} WHERE cnpj_completo = %s OR cnpj_basico = %s LIMIT 1"


@router.get("/{cnpj}", response_model=CNPJResponse)
async def get_cnpj(cnpj: str, request: Request):
    cnpj_clean = "".join(c for c in cnpj if c.isdigit())
    if len(cnpj_clean) not in (8, 14):
        raise HTTPException(status_code=400, detail="CNPJ deve ter 8 (base) ou 14 dígitos")

    async with request.app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_SELECT, (cnpj_clean, cnpj_clean))
            row = await cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail=f"CNPJ {cnpj_clean} não encontrado")
    return CNPJResponse(**row)
