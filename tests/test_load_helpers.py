"""
Testes unitários dos helpers do load_step.

_check_staging_volume foi removido junto com a estratégia de diff-merge incremental.
Este arquivo mantém apenas os testes de _read_header, que continua em uso no swap.
"""
import csv
from pathlib import Path

from steps.load_step import _read_header


class TestReadHeader:
    def test_le_header_correto(self, tmp_path):
        csv_file = tmp_path / "empresas.csv"
        with open(csv_file, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["cnpj_basico", "razao_social", "porte"])
            writer.writerow(["11111111", "EMPRESA A", "03"])
        assert _read_header(csv_file) == ["cnpj_basico", "razao_social", "porte"]

    def test_header_de_lookup(self, tmp_path):
        csv_file = tmp_path / "cnaes.csv"
        with open(csv_file, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["codigo", "descricao"])
            writer.writerow(["6201500", "DESENVOLVIMENTO DE SISTEMAS"])
        assert _read_header(csv_file) == ["codigo", "descricao"]
