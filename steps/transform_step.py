"""
Transform step — normalize Receita Federal CSV files.
- Encoding: latin-1 → utf-8
- Separator: ';' → ','
- Columns: fixed positions per RF layout (no header in source files)
- Output: clean CSVs with headers in data/{run_key}/transformed/
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from config import settings
from logger import get_logger
from steps.base import StepResult

log = get_logger("step.transform")

RF_ENCODING = "latin-1"
RF_SEP = ";"

# RF files have NO headers — column order is fixed per layout spec.
TABLE_SCHEMAS: dict[str, dict] = {
    "empresas": {
        "columns": [
            "cnpj_basico", "razao_social", "natureza_juridica",
            "qualificacao_responsavel", "capital_social", "porte",
            "ente_federativo_responsavel",
        ],
        "filename_keyword": "Empresas",
    },
    "estabelecimentos": {
        "columns": [
            "cnpj_basico", "cnpj_ordem", "cnpj_dv", "identificador_matriz_filial",
            "nome_fantasia", "situacao_cadastral", "data_situacao_cadastral",
            "motivo_situacao_cadastral", "nm_cidade_exterior", "pais",
            "data_inicio_atividade", "cnae_fiscal", "cnae_fiscal_secundaria",
            "tipo_logradouro", "logradouro", "numero", "complemento",
            "bairro", "cep", "uf", "municipio", "ddd1", "telefone1",
            "ddd2", "telefone2", "ddd_fax", "fax",
            "correio_eletronico", "situacao_especial", "data_situacao_especial",
        ],
        "filename_keyword": "Estabelecimentos",
    },
    "socios": {
        "columns": [
            "cnpj_basico", "identificador_socio", "nome_socio",
            "cnpj_cpf_socio", "qualificacao_socio",
            "data_entrada_sociedade", "pais", "representante_legal",
            "nome_representante", "qualificacao_representante", "faixa_etaria",
        ],
        "filename_keyword": "Socios",
    },
    "cnaes":          {"columns": ["codigo", "descricao"], "filename_keyword": "Cnaes"},
    "municipios":     {"columns": ["codigo", "descricao"], "filename_keyword": "Municipios"},
    "naturezas":      {"columns": ["codigo", "descricao"], "filename_keyword": "Naturezas"},
    "qualificacoes":  {"columns": ["codigo", "descricao"], "filename_keyword": "Qualificacoes"},
    "motivos":        {"columns": ["codigo", "descricao"], "filename_keyword": "Motivos"},
    "paises":         {"columns": ["codigo", "descricao"], "filename_keyword": "Paises"},
    "portes":         {"columns": ["codigo", "descricao"], "filename_keyword": "Portes"},
}

# Null-like values that should become empty string
_NULL_VALUES = frozenset({"00000000", "0001-01-01", "N/A", "NA", "000"})


def _clean(value: str) -> str:
    v = value.strip()
    return "" if v in _NULL_VALUES else v


def _find_csvs(csv_dir: Path, keyword: str) -> list[Path]:
    """Find all files in csv_dir whose name contains keyword (case-insensitive)."""
    return sorted([
        p for p in csv_dir.iterdir()
        if keyword.lower() in p.name.lower()
        and p.suffix.upper() in (".CSV", "")
        and not p.name.endswith(".tmp")
    ])


def _transform_table(csv_dir: Path, schema: dict, out_dir: Path) -> dict:
    keyword = schema["filename_keyword"]
    columns = schema["columns"]
    sources = _find_csvs(csv_dir, keyword)

    if not sources:
        log.warning(f"no CSV found for keyword '{keyword}' — table will be empty")
        return {"keyword": keyword, "rows": 0, "files": [], "out": None}

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{keyword.lower()}.csv"
    tmp_path = out_path.with_suffix(".tmp")

    total = 0
    with open(tmp_path, "w", encoding="utf-8", newline="") as out_f:
        writer = csv.writer(out_f, delimiter=",", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(columns)

        for src in sources:
            log.info(f"  transforming {src.name}...")
            with open(src, "r", encoding=RF_ENCODING, errors="replace", newline="") as in_f:
                reader = csv.reader(in_f, delimiter=RF_SEP)
                for row in reader:
                    # Pad or trim to expected column count
                    padded = (row + [""] * len(columns))[: len(columns)]
                    writer.writerow([_clean(v) for v in padded])
                    total += 1

    tmp_path.replace(out_path)
    log.info(f"  {keyword}: {total:,} rows → {out_path.name}")
    return {"keyword": keyword, "rows": total, "out": str(out_path),
            "files": [str(s) for s in sources]}


def run(job_id: str, run_key: str, checkpoint_dir: Path) -> StepResult:
    extract_artifact = checkpoint_dir / "extract_manifest.json"
    if not extract_artifact.exists():
        return StepResult.failed("extract_manifest.json missing — run extract step first")

    manifest = json.loads(extract_artifact.read_text(encoding="utf-8"))
    csv_dir = Path(manifest["csv_dir"])
    out_dir = settings.data_dir / run_key / "transformed"

    results = []
    for table_name, schema in TABLE_SCHEMAS.items():
        try:
            result = _transform_table(csv_dir, schema, out_dir)
            results.append(result)
        except Exception as exc:
            import traceback
            return StepResult.failed(
                f"transform failed for table '{table_name}': {exc}\n{traceback.format_exc()}"
            )

    artifact = checkpoint_dir / "transform_manifest.json"
    tmp = artifact.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"run_key": run_key, "out_dir": str(out_dir), "tables": results},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(artifact)

    total_rows = sum(r["rows"] for r in results)
    log.info(f"transform complete: {total_rows:,} total rows across {len(results)} tables")
    return StepResult.success(artifact_path=artifact, total_rows=total_rows)
