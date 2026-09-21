import hashlib
import json
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from psycopg.rows import dict_row

from api.limiter import limiter
from api.schemas import CNPJWithSociosResponse, SocioResponse
from api.security import verify_api_key
from config import settings

router = APIRouter(dependencies=[Depends(verify_api_key)])

_MV      = f"{settings.pg_serving_schema}.mv_cnpj_full"
_SOCIOS  = f"{settings.pg_schema}.rf_socios"
_TIMEOUT = f"SET LOCAL statement_timeout = {settings.db_query_timeout_ms}"


async def _redis_get(redis, key: str) -> Optional[str]:
    try:
        return await redis.get(key)
    except Exception:
        return None


async def _redis_set(redis, key: str, value: str) -> None:
    """Grava chave primária (TTL busca) e stale (90d) com degradação graceful."""
    try:
        await redis.setex(key, settings.search_cache_ttl, value)
        await redis.setex(f"stale:{key}", settings.cache_stale_ttl, value)
    except Exception:
        pass


async def _redis_incr(redis, key: str) -> None:
    try:
        await redis.incr(key)
    except Exception:
        pass


@router.get("/buscar", response_model=list[CNPJWithSociosResponse])
@limiter.limit(settings.rate_limit_search)
async def buscar_por_cpf_parcial(
    request: Request,
    response: Response,
    digitos: str,
    nome: str,
):
    """
    Retorna todas as empresas onde uma pessoa física aparece como sócia.

    Usa os 6 dígitos visíveis do CPF mascarado (formato RF: ***XXXXXX**)
    combinados com o nome completo do sócio para identificar a pessoa.
    A combinação é praticamente única na base da Receita Federal.
    """
    if not digitos.isdigit() or len(digitos) != 6:
        raise HTTPException(
            status_code=400,
            detail="digitos deve conter exatamente 6 dígitos numéricos"
        )
    if not nome.strip():
        raise HTTPException(status_code=400, detail="nome não pode ser vazio")

    nome_upper = nome.strip().upper()
    cpf_mascarado = f"***{digitos}**"

    cache_key = "socios_busca:" + hashlib.md5(
        f"{cpf_mascarado}|{nome_upper}".encode()
    ).hexdigest()

    cached = await _redis_get(request.app.state.redis, cache_key)
    if cached:
        await _redis_incr(request.app.state.redis, "stats:cache_hits")
        return [CNPJWithSociosResponse(**r) for r in json.loads(cached)]

    await _redis_incr(request.app.state.redis, "stats:cache_misses")
    try:
        async with request.app.state.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(_TIMEOUT)
                await cur.execute(
                    f"""
                    SELECT mv.*
                    FROM {_MV} mv
                    WHERE mv.cnpj_basico IN (
                        SELECT cnpj_basico
                        FROM {_SOCIOS}
                        WHERE identificador_socio = '2'
                          AND cnpj_cpf_socio = %s
                          AND nome_socio = %s
                    )
                    ORDER BY mv.razao_social
                    LIMIT 100
                    """,
                    (cpf_mascarado, nome_upper),
                )
                rows = await cur.fetchall()

                if not rows:
                    return []

                cnpj_basicos = list({r["cnpj_basico"] for r in rows})
                placeholders = ", ".join("%s" for _ in cnpj_basicos)
                await cur.execute(
                    f"SELECT * FROM {_SOCIOS} "
                    f"WHERE cnpj_basico IN ({placeholders}) "
                    f"ORDER BY cnpj_basico, nome_socio",
                    cnpj_basicos,
                )
                socios_rows = await cur.fetchall()
    except Exception as exc:
        stale = await _redis_get(request.app.state.redis, f"stale:{cache_key}")
        if stale:
            response.headers["X-Cache"] = "STALE"
            return [CNPJWithSociosResponse(**r) for r in json.loads(stale)]
        detail = (
            "Banco de dados sobrecarregado. Tente novamente."
            if "statement timeout" in str(exc).lower()
            else "Erro ao consultar banco de dados."
        )
        raise HTTPException(status_code=503, detail=detail)

    socios_by_cnpj: dict[str, list] = defaultdict(list)
    for s in socios_rows:
        socios_by_cnpj[s["cnpj_basico"].strip()].append(SocioResponse(**s))

    result = [
        CNPJWithSociosResponse(
            **r,
            socios=socios_by_cnpj.get(r["cnpj_basico"].strip(), []),
        )
        for r in rows
    ]

    await _redis_set(
        request.app.state.redis,
        cache_key,
        json.dumps([r.model_dump() for r in result], default=str),
    )

    return result
