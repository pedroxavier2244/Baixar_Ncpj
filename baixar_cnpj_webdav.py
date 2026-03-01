from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

# seu link público
SHARE_PAGE = "https://arquivos.receitafederal.gov.br/index.php/s/YggdBLfdninEJX9"
DEST = Path("cnpj_files")
DEST.mkdir(exist_ok=True)

TIMEOUT = 60
CHUNK = 1024 * 1024
MAX_RETRIES = 5


def get_share_token(share_url: str) -> str:
    # token é a última parte do path
    p = urlparse(share_url).path.rstrip("/")
    token = p.split("/")[-1]
    if not token:
        raise RuntimeError("Não consegui extrair o token do link.")
    return token


def webdav_base(share_url: str) -> str:
    # ex: https://arquivos.receitafederal.gov.br/public.php/webdav/
    u = urlparse(share_url)
    return f"{u.scheme}://{u.netloc}/public.php/webdav/"


def propfind_list(session: requests.Session, base: str) -> list[str]:
    """
    Lista arquivos no share via WebDAV PROPFIND.
    Retorna nomes (paths) encontrados.
    """
    headers = {"Depth": "1"}
    body = """<?xml version="1.0"?>
    <d:propfind xmlns:d="DAV:">
      <d:prop>
        <d:displayname />
        <d:getcontentlength />
      </d:prop>
    </d:propfind>"""

    r = session.request("PROPFIND", base, headers=headers, data=body, timeout=TIMEOUT)
    if r.status_code not in (207, 200):
        raise RuntimeError(f"PROPFIND falhou: HTTP {r.status_code}")

    # Extrai nomes simples do XML (sem depender de lib extra)
    # Procura por <d:href>...</d:href>
    hrefs = re.findall(r"<d:href>(.*?)</d:href>", r.text)
    # remove a própria pasta raiz
    cleaned = []
    for h in hrefs:
        h = h.strip()
        if h.endswith("/public.php/webdav/") or h.endswith("/public.php/webdav"):
            continue
        cleaned.append(h)
    return cleaned


def download(session: requests.Session, url: str, out: Path) -> None:
    if out.exists() and out.stat().st_size > 0:
        print("Já existe:", out.name)
        return

    tmp = out.with_suffix(out.suffix + ".part")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print("Baixando:", out.name)
            with session.get(url, stream=True, timeout=TIMEOUT) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(CHUNK):
                        if chunk:
                            f.write(chunk)
            tmp.replace(out)
            print("OK:", out.name)
            return
        except Exception as e:
            print(f"Falhou {attempt}/{MAX_RETRIES}: {e}")
            time.sleep(2 * attempt)

    if tmp.exists():
        tmp.unlink(missing_ok=True)


def main():
    token = get_share_token(SHARE_PAGE)
    base = webdav_base(SHARE_PAGE)

    with requests.Session() as s:
        s.headers.update({"User-Agent": "cnpj-downloader/1.0"})
        # auth padrão de share público WebDAV: usuário qualquer, senha = token
        s.auth = ("", token)

        print("WebDAV base:", base)
        hrefs = propfind_list(s, base)
        if not hrefs:
            raise RuntimeError("Não retornou itens no WebDAV. Pode ser bloqueio de rede.")

        # Filtra só .zip
        zips = []
        for h in hrefs:
            name = h.split("/")[-1]
            if name.lower().endswith(".zip"):
                zips.append(name)

        if not zips:
            print("Itens retornados:", hrefs[:10])
            raise RuntimeError("Não encontrei .zip no WebDAV.")

        print(f"Encontrados {len(zips)} ZIPs.")

        for name in sorted(zips, key=str.lower):
            url = base + name
            download(s, url, DEST / name)

        print("\nFinalizado. Pasta:", DEST.resolve())


if __name__ == "__main__":
    main()
