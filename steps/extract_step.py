"""
Extract step — unzip downloaded ZIPs into organized CSV files.
Uses streaming extraction (8 MB chunks) — no full file loaded into RAM.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from config import settings
from logger import get_logger
from steps.base import StepResult

log = get_logger("step.extract")

CHUNK = 8 * 1024 * 1024  # 8 MB


def _extract_zip(zip_path: Path, dest_dir: Path) -> list[str]:
    """
    Extract all members of zip_path into dest_dir.
    Flattens directory structure — only the filename is used.
    Skips members that already exist with non-zero size.
    Returns list of extracted filenames.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    extracted = []

    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            # Flatten path: only use the last component
            out_name = Path(member.filename).name
            if not out_name:
                continue  # skip directory entries
            out_path = dest_dir / out_name

            if out_path.exists() and out_path.stat().st_size > 0:
                log.info(f"  already extracted: {out_name}")
                extracted.append(out_name)
                continue

            tmp = out_path.with_suffix(out_path.suffix + ".tmp")
            try:
                with zf.open(member) as src, open(tmp, "wb") as dst:
                    while chunk := src.read(CHUNK):
                        dst.write(chunk)
                tmp.replace(out_path)
                size_mb = out_path.stat().st_size / 1e6
                log.info(f"  extracted: {out_name} ({size_mb:.1f} MB)")
                extracted.append(out_name)
            except Exception as exc:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"failed to extract {out_name} from {zip_path.name}: {exc}") from exc

    return extracted


def run(job_id: str, run_key: str, checkpoint_dir: Path) -> StepResult:
    download_artifact = checkpoint_dir / "download_manifest.json"
    if not download_artifact.exists():
        return StepResult.failed("download_manifest.json missing — run download step first")

    manifest = json.loads(download_artifact.read_text(encoding="utf-8"))
    zip_dir = settings.data_dir / run_key
    csv_dir = settings.data_dir / run_key / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)

    all_extracted: list[str] = []

    for item in manifest.get("results", []):
        zip_path = zip_dir / item["name"]
        if not zip_path.exists():
            return StepResult.failed(f"ZIP not found: {zip_path}")
        log.info(f"extracting {zip_path.name}...")
        try:
            names = _extract_zip(zip_path, csv_dir)
            all_extracted.extend(names)
        except Exception as exc:
            return StepResult.failed(str(exc))

    artifact = checkpoint_dir / "extract_manifest.json"
    tmp = artifact.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {"run_key": run_key, "csv_dir": str(csv_dir), "files": all_extracted},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tmp.replace(artifact)

    log.info(f"extract complete: {len(all_extracted)} files in {csv_dir}")
    return StepResult.success(artifact_path=artifact, extracted=len(all_extracted))
