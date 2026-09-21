import hashlib
import json
from collections import defaultdict
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg.rows import dict_row

from api.limiter import limiter
from api.schemas import CNPJWithSociosResponse, SocioResponse
from api.security import verify_api_key
from config import settings

router = APIRouter(dependencies=[Depends(verify_api_key)])

_MV      = f"{settings.pg_serving_schema}.mv_cnpj_full"
_SOCIOS  = f"{settings.pg_schema}.rf_socios"
_TIMEOUT = f"SET LOCAL statement_timeout = {settings.db_query_timeout_ms}"

_ALLOWED_ORDER = {
    "razao_social", "data_inicio_atividade", "municipio_descricao", "uf",
}


@router.get("", response_model=list[CNPJWithSociosResponse])
@limiter.limit(settings.rate_limit_search)
async def search(
    request: Request,
    razao_social: Optional[str] = None,
    municipio: Optional[str] = None,
    cnae: Optional[str] = None,
    uf: Optional[str] = None,
    situacao: Optional[str] = None,
    porte: Optional[str] = None,
    apenas_ativas: bool = False,        # atalho para situacao_cadastral = '02'
    apenas_matriz: bool = False,        # identificador_matriz_filial = '1'
    tem_telefone: bool = False,         # ddd1 + telefone1 preenchidos
    order_by: Optional[str] = None,     # campo para ordenar
    order_dir: Literal["asc", "desc"] = "asc",
    limit: int = 20,
    page: int = 1,                      # paginação
):
    """
    Busca empresas para o CRM com filtros avançados e paginação.
    Inclui quadro societário de cada empresa no resultado.
    Resultados são cacheados por 5 minutos (mesmo filtro = mesma resposta).
    """
    limit  = min(max(1, limit), 100)
    page   = max(1, page)
    offset = (page - 1) * limit

    conditions: list[str] = []
    params: list = []

    if razao_social:
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
    if porte:
        conditions.append("porte = %s")
        params.append(porte)
    if apenas_ativas:
        conditions.append("situacao_cadastral = '02'")
    if apenas_matriz:
        conditions.append("identificador_matriz_filial = '1'")
    if tem_telefone:
        conditions.append("ddd1 IS NOT NULL AND telefone1 IS NOT NULL AND telefone1 <> ''")

    where    = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    order_by = order_by if order_by in _ALLOWED_ORDER else None
    order    = f"ORDER BY {order_by} {order_dir.upper()}" if order_by else ""

    query = f"SELECT * FROM {_MV} {where} {order} LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    # Cache key baseada em todos os parâmetros da query — v2 inclui sócios
    cache_key = "search_v2:" + hashlib.md5(
        json.dumps({"q": query, "p": params}, default=str).encode()
    ).hexdigest()

    # Tenta cache — 5 minutos
    try:
        cached = await request.app.state.redis.get(cache_key)
        if cached:
            return [CNPJWithSociosResponse(**r) for r in json.loads(cached)]
    except Exception:
        pass

    try:
        async with request.app.state.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(_TIMEOUT)
                await cur.execute(query, params)
                rows = await cur.fetchall()

                if not rows:
                    return []

                # Batch query de sócios — uma única query para todos os resultados
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
        if "statement timeout" in str(exc).lower():
            raise HTTPException(status_code=503, detail="Banco de dados sobrecarregado. Tente novamente.")
        raise HTTPException(status_code=503, detail="Erro ao consultar banco de dados.")

    # Agrupa sócios por cnpj_basico
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

    try:
        await request.app.state.redis.setex(
            cache_key,
            settings.search_cache_ttl,
            json.dumps([r.model_dump() for r in result], default=str),
        )
    except Exception:
        pass

    return result
