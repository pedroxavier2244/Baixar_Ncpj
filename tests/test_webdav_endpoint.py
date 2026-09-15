"""
Regressão do endpoint WebDAV da Receita Federal.

Em 2026-09-11 o Nextcloud da RF parou de responder no endpoint legado
`/public.php/webdav/` (a conexão é encerrada sem resposta). O endpoint válido
passou a ser `/public.php/dav/files/<token>/`.

O XML abaixo é uma captura real da resposta do endpoint novo, reduzida.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

TOKEN = "faketoken12345"

# Captura real (reduzida) do PROPFIND na raiz do compartilhamento.
ROOT_XML = f"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:s="http://sabredav.org/ns" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
<d:response><d:href>/public.php/dav/files/{TOKEN}/</d:href><d:propstat><d:prop>
<d:getlastmodified>Mon, 14 Sep 2026 15:08:06 GMT</d:getlastmodified>
<d:resourcetype><d:collection/></d:resourcetype><d:getetag>&quot;6aa80dd72199a&quot;</d:getetag>
</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
<d:response><d:href>/public.php/dav/files/{TOKEN}/2026-08/</d:href><d:propstat><d:prop>
<d:getlastmodified>Thu, 14 Aug 2026 12:00:00 GMT</d:getlastmodified>
<d:resourcetype><d:collection/></d:resourcetype><d:getetag>&quot;aaa111&quot;</d:getetag>
</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
<d:response><d:href>/public.php/dav/files/{TOKEN}/2026-09/</d:href><d:propstat><d:prop>
<d:getlastmodified>Mon, 14 Sep 2026 15:08:06 GMT</d:getlastmodified>
<d:resourcetype><d:collection/></d:resourcetype><d:getetag>&quot;bbb222&quot;</d:getetag>
</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
</d:multistatus>"""

# Captura real (reduzida) do PROPFIND dentro de 2026-09/.
MONTH_XML = f"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:s="http://sabredav.org/ns" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
<d:response><d:href>/public.php/dav/files/{TOKEN}/2026-09/</d:href><d:propstat><d:prop>
<d:getlastmodified>Mon, 14 Sep 2026 15:08:06 GMT</d:getlastmodified>
<d:resourcetype><d:collection/></d:resourcetype><d:getetag>&quot;bbb222&quot;</d:getetag>
</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
<d:response><d:href>/public.php/dav/files/{TOKEN}/2026-09/Empresas0.zip</d:href><d:propstat><d:prop>
<d:getlastmodified>Mon, 14 Sep 2026 14:58:51 GMT</d:getlastmodified>
<d:getcontentlength>123456789</d:getcontentlength>
<d:resourcetype/><d:getetag>&quot;ccc333&quot;</d:getetag>
</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
<d:response><d:href>/public.php/dav/files/{TOKEN}/2026-09/Socios0.zip</d:href><d:propstat><d:prop>
<d:getlastmodified>Mon, 14 Sep 2026 14:59:29 GMT</d:getlastmodified>
<d:getcontentlength>987654321</d:getcontentlength>
<d:resourcetype/><d:getetag>&quot;ddd444&quot;</d:getetag>
</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
</d:multistatus>"""


def _fake_httpx_client(body: str):
    """Client falso que devolve `body` em qualquer PROPFIND, registrando a URL."""
    calls: list[str] = []

    resp = MagicMock()
    resp.status_code = 207
    resp.text = body
    resp.raise_for_status = MagicMock()

    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)

    def _request(method, url, **kwargs):
        calls.append(url)
        return resp

    client.request = _request
    return MagicMock(return_value=client), calls


def test_base_aponta_para_endpoint_novo():
    """A base precisa ser /public.php/dav/files/<token>/, não o legado."""
    from webdav import webdav_base

    base = webdav_base("https://arquivos.receitafederal.gov.br/index.php/s/" + TOKEN, TOKEN)
    assert base == f"https://arquivos.receitafederal.gov.br/public.php/dav/files/{TOKEN}/"
    assert "/public.php/webdav/" not in base


def test_propfind_parseia_listagem_do_endpoint_novo():
    """A listagem da raiz precisa render as pastas de mês do endpoint novo."""
    fake_client, calls = _fake_httpx_client(ROOT_XML)
    with patch.multiple("config.settings", webdav_token=TOKEN), \
         patch("enqueue_job.httpx.Client", fake_client):
        from enqueue_job import propfind_listing
        items = propfind_listing(TOKEN)

    assert calls[0].endswith(f"/public.php/dav/files/{TOKEN}/")
    nomes = [i["name"] for i in items]
    assert nomes == ["2026-08", "2026-09"]
    assert all(i["is_dir"] for i in items)
    # path é relativo à base — sem o prefixo do endpoint
    assert [i["path"] for i in items] == ["2026-08", "2026-09"]


def test_zips_do_mes_viram_paths_relativos():
    """Os ZIPs precisam sair com path relativo <mes>/<arquivo>.zip."""
    fake_client, _ = _fake_httpx_client(MONTH_XML)
    with patch.multiple("config.settings", webdav_token=TOKEN), \
         patch("enqueue_job.httpx.Client", fake_client):
        from enqueue_job import propfind_listing
        items = propfind_listing(TOKEN, rel_path="2026-09/")

    zips = [i for i in items if not i["is_dir"]]
    assert [i["name"] for i in zips] == ["Empresas0.zip", "Socios0.zip"]
    assert [i["path"] for i in zips] == ["2026-09/Empresas0.zip", "2026-09/Socios0.zip"]
    assert zips[0]["size"] == 123456789
    assert zips[0]["etag"] == "ccc333"


def test_download_reconstroi_a_url_real_do_arquivo():
    """
    Invariante que quebra download em silêncio: a base do download_step somada ao
    path do manifesto precisa reproduzir exatamente o href devolvido pela RF.
    """
    fake_client, _ = _fake_httpx_client(MONTH_XML)
    with patch.multiple("config.settings", webdav_token=TOKEN), \
         patch("enqueue_job.httpx.Client", fake_client):
        from enqueue_job import propfind_listing
        items = propfind_listing(TOKEN, rel_path="2026-09/")

    with patch.multiple("config.settings", webdav_token=TOKEN):
        from steps.download_step import _webdav_base
        base = _webdav_base()

    zip_item = next(i for i in items if i["name"] == "Empresas0.zip")
    url = base + zip_item["path"]
    assert url == (
        "https://arquivos.receitafederal.gov.br"
        f"/public.php/dav/files/{TOKEN}/2026-09/Empresas0.zip"
    )
