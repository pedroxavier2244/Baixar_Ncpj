"""Verifica que os aliases de _find_csvs batem com os nomes reais dos arquivos da RF."""
from pathlib import Path

import pytest

from steps.transform_step import TABLE_SCHEMAS, _find_csvs

# Nomes reais dos inner files extraidos dos ZIPs da RF (inspecionados em 2025-12)
REAL_FILENAMES = {
    "empresas":         ["K3241.K03200Y0.D51213.EMPRECSV"],
    "estabelecimentos": ["K3241.K03200Y0.D51213.ESTABELE"],
    "socios":           ["K3241.K03200Y0.D51213.SOCIOCSV"],
    "simples":          ["F.K03200$W.SIMPLES.CSV.D51213"],
    "cnaes":            ["F.K03200$Z.D51213.CNAECSV"],
    "municipios":       ["F.K03200$Z.D51213.MUNICCSV"],
    "naturezas":        ["F.K03200$Z.D51213.NATJUCSV"],
    "qualificacoes":    ["F.K03200$Z.D51213.QUALSCSV"],
    "motivos":          ["F.K03200$Z.D51213.MOTICSV"],
    "paises":           ["F.K03200$Z.D51213.PAISCSV"],
}


@pytest.mark.parametrize("table_name,filenames", REAL_FILENAMES.items())
def test_alias_matches_real_rf_filename(tmp_path: Path, table_name: str, filenames: list):
    """Cada tabela deve ser encontrada pelos aliases dado o filename real da RF."""
    if table_name not in TABLE_SCHEMAS:
        pytest.skip(f"{table_name} nao esta em TABLE_SCHEMAS ainda")

    for fname in filenames:
        (tmp_path / fname).touch()

    schema = TABLE_SCHEMAS[table_name]
    found = _find_csvs(tmp_path, schema["filename_aliases"])
    assert len(found) == len(filenames), (
        f"Aliases {schema['filename_aliases']!r} nao encontraram {filenames!r} "
        f"para '{table_name}'. Verifique os aliases no TABLE_SCHEMAS."
    )
