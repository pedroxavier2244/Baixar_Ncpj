"""
Endpoints do compartilhamento público WebDAV da Receita Federal.

Em 2026-09-11 a RF desativou o endpoint legado `/public.php/webdav/` — ele passou
a encerrar a conexão sem enviar resposta. O endpoint do Nextcloud atual é
`/public.php/dav/files/<token>/`, que também é o prefixo dos hrefs devolvidos no
PROPFIND.

Base e prefixo ficam juntos aqui porque precisam andar em par: se divergirem, a
listagem volta vazia e o download monta URL errada — em silêncio, nos dois casos.
"""
from __future__ import annotations

from urllib.parse import urlparse


def webdav_href_prefix(token: str) -> str:
    """Prefixo dos hrefs do PROPFIND, relativo à raiz do host."""
    return f"/public.php/dav/files/{token}/"


def webdav_base(share_url: str, token: str) -> str:
    """URL absoluta da raiz do compartilhamento, com barra no fim."""
    u = urlparse(share_url)
    return f"{u.scheme}://{u.netloc}{webdav_href_prefix(token)}"
