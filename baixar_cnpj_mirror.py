from __future__ import annotations
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SHARE_URL = "https://arquivos.receitafederal.gov.br/index.php/s/YggdBLfdninEJX9"
DEST = Path("cnpj_files_mirror")
DEST.mkdir(exist_ok=True)

TIMEOUT = 60
CHUNK = 1024 * 1024
MAX_RETRIES = 5

# padrões comuns dos arquivos
WANTED_PREFIXES = [
    "Empresas",
    "Estabelecimentos",
    "Socios",
    "Cnaes",
    "Municipios",
    "Naturezas",
    "Qualificacoes",
    "Motivos",
    "Paises",
    "Portes",
]

def get_listing_links(session: requests.Session) -> list[str]:
    # essa página geralmente retorna HTML com links de download
    html = session.get(SHARE_URL, timeout=TIMEOUT).text
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".zip" in href.lower():
            links.append(urljoin(SHARE_URL + "/", href))
    return links

def wanted_name(name: str) -> bool:
    low = name.lower()
    return any(low.startswith(p.lower()) for p in WANTED_PREFIXES) and low.endswith(".zip")

def download(url: str, dest: Path, session: requests.Session):
    if dest.exists() and dest.stat().st_size > 0:
        print("Já existe:", dest.name)
        return

    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print("Baixando:", dest.name)
            with session.get(url, stream=True, timeout=TIMEOUT) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(CHUNK):
                        if chunk:
                            f.write(chunk)
            tmp.replace(dest)
            print("OK:", dest.name)
            return
        except Exception as e:
            print(f"Falhou {attempt}/{MAX_RETRIES}: {e}")
            time.sleep(2 * attempt)

    if tmp.exists():
        tmp.unlink(missing_ok=True)

def main():
    with requests.Session() as s:
        s.headers.update({"User-Agent": "cnpj-downloader/1.0"})
        print("Abrindo mirror:", SHARE_URL)

        links = get_listing_links(s)
        if not links:
            raise RuntimeError("Não consegui listar arquivos no link de compartilhamento.")

        # filtra nomes de zip
        items = []
        for u in links:
            name = u.split("/")[-1].split("?")[0]
            if wanted_name(name):
                items.append((u, name))

        # se a listagem vier com nomes diferentes, mostra os primeiros para debug
        if not items:
            print("Não encontrei ZIPs esperados. Mostrando alguns links:")
            for u in links[:20]:
                print(" -", u)
            raise RuntimeError("Mirror não retornou nomes de ZIP no formato esperado.")

        print(f"Encontrados {len(items)} ZIPs relevantes.")
        for u, name in sorted(items, key=lambda x: x[1].lower()):
            download(u, DEST / name, s)

        print("\nFinalizado. Arquivos em:", DEST.resolve())

if __name__ == "__main__":
    main()
