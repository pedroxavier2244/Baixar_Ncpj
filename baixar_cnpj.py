from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


# ========= CONFIG =========
BASE_URL = "https://dadosabertos.rfb.gov.br/CNPJ/"
DEST_ROOT = Path("cnpj_files")                 # pasta onde tudo vai ser salvo
TIMEOUT = 60
CHUNK_SIZE = 1024 * 1024                       # 1MB
MAX_RETRIES = 5
RETRY_BACKOFF_SECONDS = 2

# Se quiser baixar SÓ o necessário, deixe essa lista.
# Se quiser baixar TUDO do mês, coloque WANT_KEYWORDS = None
WANT_KEYWORDS: Optional[list[str]] = [
    # principais (base)
    "Empresas",
    "Estabelecimentos",
    "Socios",
    # tabelas auxiliares (descrições/códigos)
    "Cnaes",
    "Municipios",
    "Naturezas",
    "Qualificacoes",
    "Motivos",
    "Paises",
    "Portes",
]
# ==========================


@dataclass(frozen=True)
class DownloadItem:
    url: str
    filename: str
    size_bytes: Optional[int] = None


def _get_html(url: str, session: requests.Session) -> str:
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def _list_links(url: str, session: requests.Session) -> list[str]:
    html = _get_html(url, session)
    soup = BeautifulSoup(html, "html.parser")
    return [urljoin(url, a["href"]) for a in soup.find_all("a", href=True)]


def _find_latest_month_folder(session: requests.Session) -> str:
    """
    Encontra a pasta mais recente no formato YYYY-MM/ dentro do BASE_URL.
    """
    links = _list_links(BASE_URL, session)
    months = set()

    for link in links:
        # pega só o final do link
        tail = link.rstrip("/").split("/")[-1]
        if re.fullmatch(r"\d{4}-\d{2}", tail):
            months.add(tail)

    if not months:
        raise RuntimeError(
            "Não encontrei pastas YYYY-MM no BASE_URL. "
            "Talvez o site esteja bloqueando listagem ou mudou o formato."
        )

    return sorted(months)[-1]


def _select_zip_files(month_url: str, session: requests.Session) -> list[DownloadItem]:
    """
    Lista todos os .zip dentro do mês e filtra por WANT_KEYWORDS (se definida).
    """
    links = _list_links(month_url, session)

    zip_urls = [u for u in links if u.lower().endswith(".zip")]

    if WANT_KEYWORDS is not None:
        def wanted(u: str) -> bool:
            low = u.lower()
            return any(k.lower() in low for k in WANT_KEYWORDS)

        zip_urls = [u for u in zip_urls if wanted(u)]

    items: list[DownloadItem] = []
    for u in sorted(zip_urls):
        filename = u.split("/")[-1]
        items.append(DownloadItem(url=u, filename=filename))

    return items


def _head_size(url: str, session: requests.Session) -> Optional[int]:
    """
    Tenta descobrir o tamanho via HEAD.
    Nem todo servidor retorna Content-Length.
    """
    try:
        r = session.head(url, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code >= 400:
            return None
        cl = r.headers.get("Content-Length")
        return int(cl) if cl and cl.isdigit() else None
    except Exception:
        return None


def _download_with_retries(item: DownloadItem, dest_path: Path, session: requests.Session) -> dict:
    """
    Baixa com retry, mostra progresso e salva em .part antes de concluir.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    if dest_path.exists() and dest_path.stat().st_size > 0:
        return {
            "file": str(dest_path),
            "url": item.url,
            "status": "skipped_exists",
            "bytes": dest_path.stat().st_size,
        }

    tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")

    size = item.size_bytes
    if size is None:
        size = _head_size(item.url, session)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"\n➡️  Baixando: {item.filename}")
            if size:
                print(f"   Tamanho: {size / (1024 * 1024):.2f} MB")

            with session.get(item.url, stream=True, timeout=TIMEOUT) as r:
                r.raise_for_status()
                downloaded = 0
                start = time.time()

                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_content(CHUNK_SIZE):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)

                        # progresso simples
                        elapsed = max(time.time() - start, 0.001)
                        speed = downloaded / elapsed  # bytes/s

                        if size:
                            pct = downloaded * 100 / size
                            print(
                                f"\r   {pct:6.2f}%  {downloaded/1e6:8.1f} MB  "
                                f"{speed/1e6:6.2f} MB/s",
                                end="",
                            )
                        else:
                            print(
                                f"\r   {downloaded/1e6:8.1f} MB  {speed/1e6:6.2f} MB/s",
                                end="",
                            )

            # finaliza arquivo
            tmp_path.replace(dest_path)
            print("\n✅ Concluído:", dest_path.name)

            return {
                "file": str(dest_path),
                "url": item.url,
                "status": "downloaded",
                "bytes": dest_path.stat().st_size,
            }

        except Exception as e:
            print(f"\n⚠️  Falhou (tentativa {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                sleep_s = RETRY_BACKOFF_SECONDS * attempt
                print(f"   Re-tentando em {sleep_s}s...")
                time.sleep(sleep_s)
            else:
                # limpa parcial
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except Exception:
                        pass
                return {
                    "file": str(dest_path),
                    "url": item.url,
                    "status": "failed",
                    "error": str(e),
                }


def main() -> None:
    DEST_ROOT.mkdir(exist_ok=True)

    with requests.Session() as session:
        session.headers.update({"User-Agent": "cnpj-downloader/1.0"})

        print("Conectando em:", BASE_URL)

        month = _find_latest_month_folder(session)
        month_url = urljoin(BASE_URL, f"{month}/")

        print("✅ Mês mais recente encontrado:", month)
        print("📂 URL do mês:", month_url)

        items = _select_zip_files(month_url, session)

        if not items:
            raise RuntimeError("Nenhum ZIP encontrado com os filtros atuais.")

        month_dest = DEST_ROOT / month
        month_dest.mkdir(parents=True, exist_ok=True)

        print(f"📦 Arquivos para baixar: {len(items)}")
        for it in items:
            print(" -", it.filename)

        manifest = {
            "base_url": BASE_URL,
            "month": month,
            "month_url": month_url,
            "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "filters": WANT_KEYWORDS,
            "results": [],
        }

        for item in items:
            # tenta saber o tamanho antes
            size = _head_size(item.url, session)
            item = DownloadItem(url=item.url, filename=item.filename, size_bytes=size)

            dest_path = month_dest / item.filename
            res = _download_with_retries(item, dest_path, session)
            manifest["results"].append(res)

        manifest_path = month_dest / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

        ok = sum(1 for r in manifest["results"] if r.get("status") in ("downloaded", "skipped_exists"))
        fail = sum(1 for r in manifest["results"] if r.get("status") == "failed")

        print("\n==============================")
        print("✅ Finalizado")
        print(f"OK: {ok} | Falhas: {fail}")
        print("Manifest:", manifest_path)
        print("==============================\n")


if __name__ == "__main__":
    main()
