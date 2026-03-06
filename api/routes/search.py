from typing import Optional

from fastapi import APIRouter, Request
from psycopg.rows import dict_row

from api.schemas import CNPJResponse
from config import settings

router = APIRouter()

_MV = f"{settings.pg_serving_schema}.mv_cnpj_full"


@router.get("", response_model=list[CNPJResponse])
async def search(
    request: Request,
    razao_social: Optional[str] = None,
    municipio: Optional[str] = None,
    cnae: Optional[str] = None,
    uf: Optional[str] = None,
    situacao: Optional[str] = None,
    limit: int = 20,
):
    limit = min(max(1, limit), 100)
    conditions: list[str] = []
    params: list = []

    if razao_social:
        # Two %s placeholders → same value passed twice
        conditions.append("(razao_social ILIKE %s OR nome_fantasia ILIKE %s)")
        params.extend([f"%{razao_social}%", f"%{razao_social}%"])
    if municipio:
        conditions.append("municipio_descricao ILIKE %s")
        params.append(f"%{municipio}%")
    if cnae:
        conditions.append("cnae_fiscal = %s")
        params.append(cnae)
    if uf:
        conditions.append("uf = %s")
        params.append(uf.upper())
    if situacao:
        conditions.append("situacao_cadastral = %s")
        params.append(situacao)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    query = f"SELECT * FROM {_MV} {where} LIMIT %s"
    params.append(limit)

    async with request.app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()

    return [CNPJResponse(**r) for r in rows]
