import json
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from psycopg.rows import dict_row

from api.limiter import limiter
from api.schemas import CNPJResponse, CNPJWithSociosResponse, SocioResponse
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


async def _redis_set(redis, key: str, value: str) -> None:
    """Grava chave primária (30d) e stale (90d) com degradação graceful."""
    try:
        await redis.setex(key, settings.cache_ttl, value)
        await redis.setex(f"stale:{key}", settings.cache_stale_ttl, value)
    except Exception:
        pass


async def _redis_incr(redis, key: str) -> None:
    """Incrementa contador no Redis com degradação graceful."""
    try:
        await redis.incr(key)
    except Exception:
        pass


@router.get("/{cnpj}", response_model=CNPJResponse)
@limiter.limit(settings.rate_limit_cnpj)
async def get_cnpj(cnpj: str, request: Request, response: Response):
    """
    Retorna dados completos de um CNPJ para exibição no card do CRM.
    Aceita CNPJ completo (14 dígitos) ou base (8 dígitos).
    """
    cnpj_clean = "".join(c for c in cnpj if c.isdigit())
    if len(cnpj_clean) not in (8, 14):
        raise HTTPException(status_code=400, detail="CNPJ deve ter 8 (base) ou 14 dígitos")

    cache_key = f"cnpj:{cnpj_clean}"

    # 1. Cache hit — responde em ~2ms sem tocar no banco
    cached = await _redis_get(request.app.state.redis, cache_key)
    if cached:
        await _redis_incr(request.app.state.redis, "stats:cache_hits")
        return CNPJResponse(**json.loads(cached))

    # 2. Cache miss — busca no banco com timeout de segurança
    await _redis_incr(request.app.state.redis, "stats:cache_misses")
    try:
        async with request.app.state.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(_TIMEOUT)
                await cur.execute(_SELECT, (cnpj_clean, cnpj_clean))
                row = await cur.fetchone()
    except Exception as exc:
        # Banco indisponível — tenta servir dado stale do Redis
        stale = await _redis_get(request.app.state.redis, f"stale:{cache_key}")
        if stale:
            response.headers["X-Cache"] = "STALE"
            return CNPJResponse(**json.loads(stale))
        detail = (
            "Banco de dados sobrecarregado. Tente novamente."
            if "statement timeout" in str(exc).lower()
            else "Erro ao consultar banco de dados."
        )
        raise HTTPException(status_code=503, detail=detail)

    if not row:
        raise HTTPException(status_code=404, detail=f"CNPJ {cnpj_clean} não encontrado")

    result = CNPJResponse(**row)

    # 3. Salva no cache — chave primária (30d) + stale (90d)
    await _redis_set(
        request.app.state.redis,
        cache_key,
        json.dumps(result.model_dump(), default=str),
    )

    return result


@router.get("/{cnpj}/socios", response_model=list[SocioResponse])
@limiter.limit(settings.rate_limit_cnpj)
async def get_socios(cnpj: str, request: Request, response: Response):
    """
    Retorna os sócios/administradores de uma empresa.
    Exibido no card do CRM na seção 'Quadro Societário'.
    """
    cnpj_clean = "".join(c for c in cnpj if c.isdigit())
    if len(cnpj_clean) not in (8, 14):
        raise HTTPException(status_code=400, detail="CNPJ deve ter 8 (base) ou 14 dígitos")

    cnpj_basico = cnpj_clean[:8]
    cache_key = f"socios:{cnpj_basico}"

    cached = await _redis_get(request.app.state.redis, cache_key)
    if cached:
        await _redis_incr(request.app.state.redis, "stats:cache_hits")
        return [SocioResponse(**s) for s in json.loads(cached)]

    await _redis_incr(request.app.state.redis, "stats:cache_misses")
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
        stale = await _redis_get(request.app.state.redis, f"stale:{cache_key}")
        if stale:
            response.headers["X-Cache"] = "STALE"
            return [SocioResponse(**s) for s in json.loads(stale)]
        detail = (
            "Banco de dados sobrecarregado. Tente novamente."
            if "statement timeout" in str(exc).lower()
            else "Erro ao consultar banco de dados."
        )
        raise HTTPException(status_code=503, detail=detail)

    result = [SocioResponse(**r) for r in rows]

    await _redis_set(
        request.app.state.redis,
        cache_key,
        json.dumps([s.model_dump() for s in result], default=str),
    )

    return result


@router.get("/{cnpj}/participacoes", response_model=list[CNPJWithSociosResponse])
@limiter.limit(settings.rate_limit_cnpj)
async def get_participacoes(cnpj: str, request: Request, response: Response):
    """
    Retorna todas as empresas onde este CNPJ aparece como sócio (PJ).
    Útil para expandir nós de empresa no grafo de vínculos societários.
    Requer CNPJ completo (14 dígitos).
    """
    cnpj_clean = "".join(c for c in cnpj if c.isdigit())
    if len(cnpj_clean) != 14:
        raise HTTPException(status_code=400, detail="CNPJ deve ter 14 dígitos para busca de participações")

    cache_key = f"participacoes:{cnpj_clean}"

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
                    SELECT DISTINCT mv.*
                    FROM {_MV} mv
                    INNER JOIN {_SOCIOS} s ON s.cnpj_basico = mv.cnpj_basico
                    WHERE s.identificador_socio = '1'
                      AND s.cnpj_cpf_socio = %s
                    ORDER BY mv.razao_social
                    LIMIT 100
                    """,
                    (cnpj_clean,),
                )
                rows = await cur.fetchall()

                if not rows:
                    return []

                # Batch fetch de sócios para cada empresa encontrada
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
