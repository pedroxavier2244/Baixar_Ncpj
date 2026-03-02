"""
Dry-run local do ETL CNPJ sem PostgreSQL.

Executa apenas:
  1) download
  2) verify
  3) extract
  4) transform

Uso:
  python dry_run_local.py
  python dry_run_local.py --run-key 2026-02 --max-files 3
  python dry_run_local.py --full
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from enqueue_job import _detect_run_key, _filter_wanted, _save_manifest, propfind_listing
from logger import get_logger
from steps.download_step import run as run_download
from steps.extract_step import run as run_extract
from steps.transform_step import run as run_transform
from steps.verify_step import run as run_verify

log = get_logger("dry_run_local")


def _list_month_files(run_key: str) -> list[dict]:
    root_items = propfind_listing(settings.webdav_token)
    root_wanted = _filter_wanted(root_items)
    if root_wanted:
        return root_wanted

    month_items = propfind_listing(settings.webdav_token, rel_path=f"{run_key}/")
    return _filter_wanted(month_items)


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run local ETL (sem PostgreSQL)")
    parser.add_argument("--run-key", help="YYYY-MM (opcional; default auto-detect)")
    parser.add_argument(
        "--max-files",
        type=int,
        default=2,
        help="Quantidade máxima de ZIPs para teste rápido (default: 2)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Processa todos os ZIPs do mês (ignora --max-files)",
    )
    args = parser.parse_args()

    settings.ensure_dirs()
    if not settings.webdav_token:
        log.error("WEBDAV_TOKEN não configurado no .env")
        return 1

    log.info("listando arquivos no WebDAV...")
    try:
        root_items = propfind_listing(settings.webdav_token)
    except Exception as exc:
        log.error(f"falha no PROPFIND: {exc}")
        return 1

    run_key = args.run_key or _detect_run_key(root_items)
    files = _list_month_files(run_key)
    if not files:
        log.error(f"nenhum ZIP desejado encontrado para run_key={run_key}")
        return 1

    if not args.full:
        files = sorted(files, key=lambda x: (x.get("size", 0), x.get("name", "")))[: max(1, args.max_files)]
        log.info(f"modo rápido: processando {len(files)} arquivo(s)")
    else:
        log.info(f"modo completo: processando {len(files)} arquivo(s)")

    _save_manifest(run_key, files)
    log.info(f"manifest salvo para run_key={run_key}")

    job_id = f"dryrun-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    checkpoint_dir: Path = settings.checkpoint_dir / job_id
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    for step_name, step_fn in [
        ("download", run_download),
        ("verify", run_verify),
        ("extract", run_extract),
        ("transform", run_transform),
    ]:
        log.info(f"[{step_name}] iniciando")
        result = step_fn(job_id, run_key, checkpoint_dir)
        if result.status.value != "SUCCESS":
            log.error(f"[{step_name}] falhou: {result.error}")
            return 1
        log.info(f"[{step_name}] ok")

    log.info(f"dry-run finalizado com sucesso. checkpoint={checkpoint_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
