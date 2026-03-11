import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg.rows import dict_row

from api.limiter import limiter
from api.schemas import CNPJResponse, SocioResponse
from api.security import verify_api_key
from config import settings

router = APIRouter(dependencies=[Depends(verify_api_key)])

_MV      = f"{settings.pg_serving_schema}.mv_cnpj_full"
_SOCIOS  = f"{settings.pg_schema}.rf_socios"
_SELECT  = f"SELECT * FROM {_MV} WHERE cnpj_completo = %s OR cnpj_basico = %s LIMIT 1"
_TIMEOUT = f"SET LOCAL statement_timeout = {settings.db_query_timeout_ms}"


async def _redis_get(redis, key: str) -> Optional[str]:
    """Lê do Redis com degradação graceful — se Redis cair, retorna None."""
    try:
        return await redis.get(key)
    except Exception:
        return None


async def _redis_set(redis, key: str, ttl: int, value: str) -> None:
    """Grava no Redis com degradação graceful — se Redis cair, ignora."""
    try:
        await redis.setex(key, ttl, value)
    except Exception:
        pass


@router.get("/{cnpj}", response_model=CNPJResponse)
@limiter.limit(settings.rate_limit_cnpj)
async def get_cnpj(cnpj: str, request: Request):
    """
    Retorna dados completos de um CNPJ para exibição no card do CRM.
    Aceita CNPJ completo (14 dígitos) ou base (8 dígitos).
    """
    cnpj_clean = "".join(c for c in cnpj if c.isdigit())
    if len(cnpj_clean) not in (8, 14):
        raise HTTPException(status_code=400, detail="CNPJ deve ter 8 (base) ou 14 dígitos")

    cache_key = f"cnpj:{cnpj_clean}"

    # 1. Cache Redis — responde em ~2ms sem tocar no banco
    cached = await _redis_get(request.app.state.redis, cache_key)
    if cached:
        return CNPJResponse(**json.loads(cached))

    # 2. Cache miss — busca no banco com timeout de segurança
    try:
        async with request.app.state.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(_TIMEOUT)
                await cur.execute(_SELECT, (cnpj_clean, cnpj_clean))
                row = await cur.fetchone()
    except Exception as exc:
        if "statement timeout" in str(exc).lower():
            raise HTTPException(status_code=503, detail="Banco de dados sobrecarregado. Tente novamente.")
        raise HTTPException(status_code=503, detail="Erro ao consultar banco de dados.")

    if not row:
        raise HTTPException(status_code=404, detail=f"CNPJ {cnpj_clean} não encontrado")

    result = CNPJResponse(**row)

    # 3. Salva no cache por 24h
    await _redis_set(
        request.app.state.redis,
        cache_key,
        settings.cache_ttl,
        json.dumps(result.model_dump(), default=str),
    )

    return result


@router.get("/{cnpj}/socios", response_model=list[SocioResponse])
@limiter.limit(settings.rate_limit_cnpj)
async def get_socios(cnpj: str, request: Request):
    """
    Retorna os sócios/administradores de uma empresa.
    Exibido no card do CRM na seção 'Quadro Societário'.
    """
    cnpj_clean = "".join(c for c in cnpj if c.isdigit())
    if len(cnpj_clean) not in (8, 14):
        raise HTTPException(status_code=400, detail="CNPJ deve ter 8 (base) ou 14 dígitos")

    # Para CNPJ completo, usa só a base (8 dígitos)
    cnpj_basico = cnpj_clean[:8]
    cache_key = f"socios:{cnpj_basico}"

    cached = await _redis_get(request.app.state.redis, cache_key)
    if cached:
        return [SocioResponse(**s) for s in json.loads(cached)]

    try:
        async with request.app.state.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(_TIMEOUT)
                await cur.execute(
                    f"SELECT * FROM {_SOCIOS} WHERE cnpj_basico = %s ORDER BY nome_socio",
                    (cnpj_basico,),
                )
                rows = await cur.fetchall()
    except Exception as exc:
        if "statement timeout" in str(exc).lower():
            raise HTTPException(status_code=503, detail="Banco de dados sobrecarregado. Tente novamente.")
        raise HTTPException(status_code=503, detail="Erro ao consultar banco de dados.")

    result = [SocioResponse(**r) for r in rows]

    await _redis_set(
        request.app.state.redis,
        cache_key,
        settings.cache_ttl,
        json.dumps([s.model_dump() for s in result], default=str),
    )

    return result
