"""
Autenticação por API Key — o CRM envia X-API-Key no header.

Se API_KEY não estiver configurada no .env, autenticação é desabilitada
(útil para desenvolvimento local).
"""
from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from config import settings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: str = Security(_api_key_header)) -> None:
    if not settings.api_key:
        return  # desabilitado — desenvolvimento
    if api_key != settings.api_key:
        raise HTTPException(
            status_code=401,
            detail="API Key inválida ou ausente. Envie o header X-API-Key.",
        )
