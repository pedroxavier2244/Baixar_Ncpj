"""
Check if a new month is published on Receita Federal Nextcloud WebDAV.
If yes (or --force), create a PENDING job in SQLite.

Usage:
    python enqueue_job.py                    # auto-detect latest month
    python enqueue_job.py --run-key 2026-02  # specific month
    python enqueue_job.py --force            # re-enqueue even if already done
    python enqueue_job.py --check-only       # just print status, no enqueue

Modos sem rede, para quando o download acontece fora desta máquina
(desde 11/09/2026 a VPS não alcança a Receita — ver docs/HANDOFF-MAC-MINI.md):

    python enqueue_job.py --status --run-key 2026-10       # JSON do job, ou null
    python enqueue_job.py --files-ready --run-key 2026-10  # confere e cria o job

Os dois nunca falam com a Receita: leem o manifest já salvo em CHECKPOINT_DIR e
o que está em DATA_DIR. Moram aqui de propósito — módulo novo na raiz obrigaria
a mexer no COPY do Dockerfile, que já derrubou o container uma vez (15/09/2026).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings
from db.control import create_job, get_job_by_run_key, init_db, requeue_job
from logger import get_logger
from webdav import webdav_base, webdav_href_prefix

log = get_logger("enqueue_job")

PROPFIND_BODY = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:displayname/>
    <d:getcontentlength/>
    <d:getlastmodified/>
    <d:getetag/>
  </d:prop>
</d:propfind>"""


def _webdav_auth_candidates(token: str) -> list[tuple[str, str]]:
    # Different Nextcloud setups accept either password=token or username=token.
    return [("", token), (token, "")]


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
def propfind_listing(token: str, rel_path: str = "") -> list[dict]:
    """Return listing items from WebDAV PROPFIND for rel_path."""
    base = webdav_base(settings.webdav_share_url, token)
    url = base + rel_path.lstrip("/")
    last_error: Exception | None = None
    r: httpx.Response | None = None
    for auth in _webdav_auth_candidates(token):
        with httpx.Client(auth=auth, timeout=60) as client:
            resp = client.request(
                "PROPFIND", url,
                headers={"Depth": "1", "Content-Type": "application/xml"},
                content=PROPFIND_BODY.encode(),
            )
        if resp.status_code == 401:
            last_error = httpx.HTTPStatusError(
                "unauthorized with attempted auth mode",
                request=resp.request,
                response=resp,
            )
            continue
        resp.raise_for_status()
        r = resp
        break

    if r is None:
        if last_error is not None:
            raise last_error
        raise RuntimeError("WebDAV PROPFIND failed without response")

    root = ET.fromstring(r.text)
    ns = {"d": "DAV:"}
    items = []
    prefix = webdav_href_prefix(token)
    for response in root.findall("d:response", ns):
        href = (response.findtext("d:href", "", ns) or "")
        if prefix not in href:
            continue
        rel = href.split(prefix, 1)[1].lstrip("/")
        if not rel:
            continue
        is_dir = rel.endswith("/")
        rel_clean = rel.rstrip("/")
        name = unquote(rel_clean.split("/")[-1])
        props = response.find("d:propstat/d:prop", ns)
        if props is None:
            continue
        items.append({
            "name": name,
            "path": rel_clean,
            "is_dir": is_dir,
            "size": int(props.findtext("d:getcontentlength", "0", ns) or 0),
            "modified": props.findtext("d:getlastmodified", "", ns),
            "etag": (props.findtext("d:getetag", "", ns) or "").strip('"'),
        })
    return items


def _detect_run_key(items: list[dict]) -> str:
    """Infer run_key (YYYY-MM) from filenames or fall back to current month."""
    month_re = re.compile(r"(\d{4})[-_]?(\d{2})", re.IGNORECASE)
    months: set[str] = set()
    for item in items:
        m = month_re.search(item["name"])
        if m:
            months.add(f"{m.group(1)}-{m.group(2)}")
    return sorted(months)[-1] if months else datetime.now(timezone.utc).strftime("%Y-%m")


def _manifest_path(run_key: str) -> Path:
    return settings.checkpoint_dir / f"webdav_manifest_{run_key}.json"


def _load_manifest(run_key: str) -> dict | None:
    p = _manifest_path(run_key)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _save_manifest(run_key: str, items: list[dict]) -> None:
    settings.ensure_dirs()
    p = _manifest_path(run_key)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"run_key": run_key, "files": items}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(p)


def _files_changed(old: dict | None, new_items: list[dict]) -> bool:
    """True if the new listing differs from the saved manifest (by etag)."""
    if old is None:
        return True
    old_map = {f["name"]: f["etag"] for f in old.get("files", [])}
    new_map = {f["name"]: f["etag"] for f in new_items}
    return old_map != new_map


def _filter_wanted(items: list[dict]) -> list[dict]:
    return [
        i for i in items
        if not i.get("is_dir")
        and i["name"].lower().endswith(".zip")
        and any(w.lower() in i["name"].lower() for w in settings.wanted_files)
    ]


def detect_wanted(run_key: str | None = None) -> tuple[str, list[dict]]:
    """
    Consulta o WebDAV da RF e devolve (run_key, lista de ZIPs desejados).
    Levanta RuntimeError se não houver token ou se nenhum ZIP for encontrado.
    Falhas de rede (PROPFIND) propagam como exceção httpx.
    """
    if not settings.webdav_token:
        raise RuntimeError("WEBDAV_TOKEN is not set in .env")

    items = propfind_listing(settings.webdav_token)
    run_key = run_key or _detect_run_key(items)
    wanted = _filter_wanted(items)
    if not wanted:
        month_items = propfind_listing(settings.webdav_token, rel_path=f"{run_key}/")
        wanted = _filter_wanted(month_items)
    if not wanted:
        raise RuntimeError(
            "no wanted ZIPs found in WebDAV listing - check token/URL and wanted_files config"
        )
    return run_key, wanted


def job_status(run_key: str) -> dict | None:
    """
    Status do job de run_key, ou None se não existir nenhum.
    Não toca a rede: é a resposta para "essa máquina já processou esse mês?".
    """
    init_db()
    row = get_job_by_run_key(run_key)
    if row is None:
        return None
    return {
        "run_key": row["run_key"],
        "job_id": row["job_id"],
        "status": row["status"],
        "attempts": row["attempts"],
        "max_attempts": row["max_attempts"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "last_error": row["last_error"],
    }


def check_local_files(run_key: str) -> dict:
    """
    Confere DATA_DIR/<run_key> contra o manifest salvo, por nome e tamanho.

    Tamanho e não checksum de propósito: o CRC de cada membro do ZIP é trabalho
    do verify_step, que roda depois. O que esta conferência precisa barrar é o
    arquivo truncado, porque o download_step trata qualquer arquivo com tamanho
    maior que zero como pronto e o pula.
    """
    manifest = _load_manifest(run_key)
    if manifest is None:
        return {"ok": False, "reason": "manifest_missing",
                "manifest_path": str(_manifest_path(run_key))}

    files = manifest.get("files", [])
    if not files:
        return {"ok": False, "reason": "manifest_empty",
                "manifest_path": str(_manifest_path(run_key))}

    data_dir = settings.data_dir / run_key
    esperados = {f["name"]: int(f["size"]) for f in files}

    problemas: list[str] = []
    total_bytes = 0
    for nome, tamanho in sorted(esperados.items()):
        p = data_dir / nome
        if not p.exists():
            problemas.append(f"falta {nome}")
            continue
        real = p.stat().st_size
        if real != tamanho:
            problemas.append(f"tamanho {nome}: {real} != {tamanho}")
            continue
        total_bytes += real

    # Arquivo fora do manifest não quebra nada — extract_step itera o manifest,
    # nunca a pasta — mas um `.part` sobrando é sinal de transporte interrompido.
    extras = (
        sorted(p.name for p in data_dir.iterdir() if p.name not in esperados)
        if data_dir.is_dir() else []
    )

    return {
        "ok": not problemas,
        "reason": None if not problemas else "files_mismatch",
        "run_key": run_key,
        "esperados": len(esperados),
        "conferidos": len(esperados) - len(problemas),
        "problemas": problemas,
        "extras": extras,
        "bytes": total_bytes,
    }


def files_ready(run_key: str, dry_run: bool = False) -> dict:
    """
    Cria o job de run_key a partir de arquivos que já estão em DATA_DIR.

    Para quando o download foi feito em outra máquina. Confere tudo contra o
    manifest antes e recusa se já existir qualquer job para o mês — reprocessar
    um DEAD é decisão manual, não automática.
    """
    init_db()

    # A existência do job vem antes da conferência de arquivos de propósito: é a
    # guarda mais forte e a mais barata. Na ordem inversa, um mês já processado
    # — cujos ZIPs o cleanup_step apagou — seria recusado por "arquivo faltando",
    # escondendo o motivo real atrás de 37 linhas de falso alarme.
    existing = get_job_by_run_key(run_key)
    if existing is not None:
        return {
            "status": "job_exists",
            "run_key": run_key,
            "job_id": existing["job_id"],
            "job_status": existing["status"],
        }

    conferencia = check_local_files(run_key)
    if not conferencia["ok"]:
        return {"status": conferencia["reason"], **conferencia}

    if conferencia["extras"]:
        log.warning(
            f"{run_key}: {len(conferencia['extras'])} arquivo(s) fora do manifest "
            f"em {settings.data_dir / run_key}: {conferencia['extras'][:5]}"
        )

    if dry_run:
        log.info(f"dry-run: {run_key} pronto para virar job, nada foi criado")
        return {"status": "dry_run_ok", **conferencia}

    manifest = _load_manifest(run_key) or {}
    job_id = create_job(run_key, payload={
        "files": manifest.get("files", []),
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
        "origem": "arquivos entregues por outra maquina (--files-ready)",
    })
    log.info(
        f"job criado job_id={job_id} run_key={run_key} "
        f"arquivos={conferencia['esperados']} bytes={conferencia['bytes']}"
    )
    return {
        "status": "created",
        "run_key": run_key,
        "job_id": job_id,
        "files": conferencia["esperados"],
        "bytes": conferencia["bytes"],
    }


def check_and_enqueue(run_key: str | None = None, force: bool = False) -> dict:
    """
    Checa o WebDAV e enfileira um job se houver dado novo/alterado.
    Nunca chama sys.exit. Retorna um dict com a chave "status":
      enqueued | requeued | already_success | no_change | error
    """
    init_db()
    settings.ensure_dirs()

    try:
        run_key, wanted = detect_wanted(run_key)
    except Exception as exc:
        log.error(f"WebDAV detection failed: {exc}")
        return {"status": "error", "reason": str(exc)}

    log.info(f"detected run_key={run_key}, files={len(wanted)}")

    existing = get_job_by_run_key(run_key)
    if existing and existing["status"] == "SUCCESS" and not force:
        log.info(f"run_key={run_key} already SUCCESS - nothing to do")
        return {"status": "already_success", "run_key": run_key}

    old_manifest = _load_manifest(run_key)
    if not _files_changed(old_manifest, wanted) and not force:
        log.info(f"files unchanged since last run of {run_key} - skipping enqueue")
        return {"status": "no_change", "run_key": run_key}

    _save_manifest(run_key, wanted)
    payload = {"files": wanted, "enqueued_at": datetime.now(timezone.utc).isoformat()}
    if existing:
        job_id = requeue_job(run_key, payload=payload)
        log.info(f"job reset to PENDING job_id={job_id} run_key={run_key}")
        status = "requeued"
    else:
        job_id = create_job(run_key, payload=payload)
        log.info(f"job created job_id={job_id} run_key={run_key}")
        status = "enqueued"
    return {"status": status, "run_key": run_key, "job_id": job_id, "files": len(wanted)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Enqueue CNPJ ETL job")
    parser.add_argument("--run-key", help="YYYY-MM to process (default: auto-detect)")
    parser.add_argument("--force", action="store_true",
                        help="Re-enqueue even if already SUCCESS")
    parser.add_argument("--check-only", action="store_true",
                        help="Only check status, do not create job")
    parser.add_argument("--status", action="store_true",
                        help="Imprime o JSON do job de --run-key (ou null). Sem rede.")
    parser.add_argument("--files-ready", action="store_true",
                        help="Confere DATA_DIR/<run-key> contra o manifest e cria o "
                             "job. Para download feito em outra maquina. Sem rede.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Com --files-ready: confere tudo mas nao cria o job")
    args = parser.parse_args()

    # ── Modos sem rede ──────────────────────────────────────────────────────
    # Vêm antes de tudo: não chamam a Receita e não devem nem logar tentativa.
    if args.status or args.files_ready:
        if args.status and args.files_ready:
            parser.error("--status e --files-ready sao mutuamente exclusivos")
        if not args.run_key:
            parser.error("--run-key AAAA-MM e obrigatorio com --status/--files-ready")
        if not re.fullmatch(r"\d{4}-\d{2}", args.run_key):
            parser.error(f"--run-key deve ser AAAA-MM, recebi {args.run_key!r}")

        if args.status:
            print(json.dumps(job_status(args.run_key), indent=2, ensure_ascii=False))
            sys.exit(0)

        settings.ensure_dirs()
        result = files_ready(args.run_key, dry_run=args.dry_run)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if result["status"] not in ("created", "dry_run_ok"):
            log.error(f"--files-ready recusou {args.run_key}: {result['status']}")
            sys.exit(1)
        sys.exit(0)

    init_db()
    settings.ensure_dirs()

    log.info("checking Receita Federal WebDAV for new files...")

    if args.check_only:
        try:
            run_key, wanted = detect_wanted(args.run_key)
        except Exception as exc:
            log.error(f"WebDAV detection failed: {exc}")
            sys.exit(1)
        print(json.dumps({"run_key": run_key, "files": wanted}, indent=2, ensure_ascii=False))
        sys.exit(0)

    result = check_and_enqueue(args.run_key, force=args.force)
    if result["status"] == "error":
        sys.exit(1)
    if result["status"] in ("enqueued", "requeued"):
        print(f"OK job_id={result['job_id']} run_key={result['run_key']}")
    sys.exit(0)


if __name__ == "__main__":
    main()

