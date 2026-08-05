"""
Testes unitários do transform_step.

Verifica:
- Função _clean() com valores nulos RF
- _find_csvs() com nomes reais de arquivos RF
- _transform_table(): encoding latin-1→utf8, separador ;→,, header, colunas obrigatórias
- run() completo com extract_manifest.json
"""
import csv
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from steps.transform_step import (
    TABLE_SCHEMAS,
    _clean,
    _find_csvs,
    _transform_table,
    run,
)


# ── _clean ────────────────────────────────────────────────────────────────────

class TestClean:
    def test_null_date(self):
        assert _clean("00000000") == ""

    def test_null_old_date(self):
        assert _clean("0001-01-01") == ""

    def test_null_na(self):
        assert _clean("N/A") == ""
        assert _clean("NA") == ""

    def test_null_zeros(self):
        assert _clean("000") == ""

    def test_strips_whitespace(self):
        assert _clean("  ALPHA LTDA  ") == "ALPHA LTDA"

    def test_keeps_valid_value(self):
        assert _clean("EMPRESA ALPHA") == "EMPRESA ALPHA"

    def test_empty_string(self):
        assert _clean("") == ""

    def test_valid_cnpj(self):
        assert _clean("11111111") == "11111111"


# ── _find_csvs ────────────────────────────────────────────────────────────────

class TestFindCsvs:
    def test_encontra_empresas_por_alias(self, tmp_path):
        (tmp_path / "K3241.K03200Y0.D51213.EMPRECSV").touch()
        result = _find_csvs(tmp_path, ["Empresas", "EMPRECSV"])
        assert len(result) == 1

    def test_encontra_estabelecimentos_por_alias(self, tmp_path):
        (tmp_path / "K3241.K03200Y0.D51213.ESTABELE").touch()
        result = _find_csvs(tmp_path, ["Estabelecimentos", "ESTABELE"])
        assert len(result) == 1

    def test_encontra_simples_por_alias(self, tmp_path):
        (tmp_path / "F.K03200$W.SIMPLES.CSV.D51213").touch()
        result = _find_csvs(tmp_path, ["Simples", "SIMPLES"])
        assert len(result) == 1

    def test_ignora_tmp_files(self, tmp_path):
        (tmp_path / "K3241.K03200Y0.D51213.EMPRECSV").touch()
        (tmp_path / "K3241.K03200Y0.D51213.EMPRECSV.tmp").touch()
        result = _find_csvs(tmp_path, ["EMPRECSV"])
        assert len(result) == 1
        assert not any(str(p).endswith(".tmp") for p in result)

    def test_retorna_vazio_sem_match(self, tmp_path):
        (tmp_path / "OUTRO_ARQUIVO.CSV").touch()
        result = _find_csvs(tmp_path, ["EMPRECSV"])
        assert result == []

    def test_case_insensitive(self, tmp_path):
        (tmp_path / "arquivo_emprecsv_teste").touch()
        result = _find_csvs(tmp_path, ["EMPRECSV"])
        assert len(result) == 1


# ── _transform_table ─────────────────────────────────────────────────────────

class TestTransformTable:
    def test_encoding_latin1_para_utf8(self, rf_csv_dir, tmp_path):
        """CSV RF em latin-1 deve ser lido e gravado em utf-8."""
        schema = TABLE_SCHEMAS["empresas"]
        out_dir = tmp_path / "transformed"
        result = _transform_table(rf_csv_dir, schema, out_dir)
        out_file = Path(result["out"])
        content = out_file.read_text(encoding="utf-8")
        assert "cnpj_basico" in content  # header presente
        assert "EMPRESA ALPHA LTDA" in content

    def test_separador_ponto_virgula_para_virgula(self, rf_csv_dir, tmp_path):
        """Saída deve usar vírgula como separador."""
        schema = TABLE_SCHEMAS["empresas"]
        out_dir = tmp_path / "transformed"
        result = _transform_table(rf_csv_dir, schema, out_dir)
        with open(result["out"], encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 3
        assert rows[0]["cnpj_basico"] == "11111111"
        assert rows[0]["razao_social"] == "EMPRESA ALPHA LTDA"

    def test_header_injetado(self, rf_csv_dir, tmp_path):
        """Arquivo de saída deve ter header (RF não tem header)."""
        schema = TABLE_SCHEMAS["empresas"]
        out_dir = tmp_path / "transformed"
        result = _transform_table(rf_csv_dir, schema, out_dir)
        with open(result["out"], encoding="utf-8") as f:
            first_line = f.readline().strip()
        assert "cnpj_basico" in first_line
        assert "razao_social" in first_line

    def test_conta_linhas_correto(self, rf_csv_dir, tmp_path):
        schema = TABLE_SCHEMAS["empresas"]
        out_dir = tmp_path / "transformed"
        result = _transform_table(rf_csv_dir, schema, out_dir)
        assert result["rows"] == 3

    def test_transform_estabelecimentos(self, rf_csv_dir, tmp_path):
        schema = TABLE_SCHEMAS["estabelecimentos"]
        out_dir = tmp_path / "transformed"
        result = _transform_table(rf_csv_dir, schema, out_dir)
        assert result["rows"] == 3
        with open(result["out"], encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert rows[0]["uf"] == "SP"
        assert rows[2]["uf"] == "MG"

    def test_transform_socios(self, rf_csv_dir, tmp_path):
        schema = TABLE_SCHEMAS["socios"]
        out_dir = tmp_path / "transformed"
        result = _transform_table(rf_csv_dir, schema, out_dir)
        assert result["rows"] == 3

    def test_transform_simples(self, rf_csv_dir, tmp_path):
        schema = TABLE_SCHEMAS["simples"]
        out_dir = tmp_path / "transformed"
        result = _transform_table(rf_csv_dir, schema, out_dir)
        assert result["rows"] == 3

    def test_transform_lookup_cnaes(self, rf_csv_dir, tmp_path):
        schema = TABLE_SCHEMAS["cnaes"]
        out_dir = tmp_path / "transformed"
        result = _transform_table(rf_csv_dir, schema, out_dir)
        assert result["rows"] == 3

    def test_arquivo_ausente_retorna_zero_linhas(self, tmp_path):
        """Se CSV não existe, retorna rows=0 sem erro (aviso no log)."""
        schema = TABLE_SCHEMAS["empresas"]
        out_dir = tmp_path / "transformed"
        result = _transform_table(tmp_path, schema, out_dir)
        assert result["rows"] == 0
        assert result["out"] is None

    def test_linhas_completamente_vazias_sao_ignoradas(self, tmp_path):
        """Linhas em branco no CSV RF não devem gerar registros."""
        csv_file = tmp_path / "K3241.K03200Y0.D51213.EMPRECSV"
        with open(csv_file, "w", encoding="latin-1") as f:
            f.write("11111111;EMPRESA A;2062;49;100000;03;\n")
            f.write(";;;;;;\n")          # linha vazia
            f.write("\n")               # linha em branco
            f.write("22222222;EMPRESA B;2062;10;200000;01;\n")

        schema = TABLE_SCHEMAS["empresas"]
        out_dir = tmp_path / "out"
        result = _transform_table(tmp_path, schema, out_dir)
        assert result["rows"] == 2
        assert result["skipped_empty"] >= 1

    def test_linha_sem_pk_obrigatoria_e_ignorada(self, tmp_path):
        """Linha sem cnpj_basico não deve ser inserida."""
        csv_file = tmp_path / "K3241.K03200Y0.D51213.EMPRECSV"
        with open(csv_file, "w", encoding="latin-1") as f:
            f.write("11111111;EMPRESA A;2062;49;100000;03;\n")
            f.write(";SEM CNPJ;2062;49;100000;03;\n")  # cnpj_basico vazio

        schema = TABLE_SCHEMAS["empresas"]
        out_dir = tmp_path / "out"
        result = _transform_table(tmp_path, schema, out_dir)
        assert result["rows"] == 1
        assert result["skipped_invalid_required"] == 1


# ── run() completo ────────────────────────────────────────────────────────────

class TestTransformRun:
    def test_run_completo(self, rf_csv_dir, tmp_path):
        """run() lê extract_manifest.json e transforma todas as tabelas."""
        checkpoint_dir = tmp_path / "checkpoints"
        checkpoint_dir.mkdir()

        extract_manifest = {
            "run_key": "2026-01",
            "csv_dir": str(rf_csv_dir),
        }
        (checkpoint_dir / "extract_manifest.json").write_text(
            json.dumps(extract_manifest), encoding="utf-8"
        )

        with patch("steps.transform_step.settings.data_dir", tmp_path):
            result = run("job-test", "2026-01", checkpoint_dir)

        assert result.status == "SUCCESS"
        assert result.metadata.get("total_rows", 0) > 0
        assert (checkpoint_dir / "transform_manifest.json").exists()

    def test_run_falha_sem_extract_manifest(self, tmp_path):
        """run() sem extract_manifest.json deve retornar FAILED."""
        checkpoint_dir = tmp_path / "checkpoints"
        checkpoint_dir.mkdir()
        result = run("job-test", "2026-01", checkpoint_dir)
        assert result.status == "FAILED"
        assert "extract_manifest" in result.error.lower()

    def test_transform_manifest_contem_todas_tabelas(self, rf_csv_dir, tmp_path):
        """Manifest gerado deve listar todas as tabelas transformadas."""
        checkpoint_dir = tmp_path / "checkpoints"
        checkpoint_dir.mkdir()
        (checkpoint_dir / "extract_manifest.json").write_text(
            json.dumps({"run_key": "2026-01", "csv_dir": str(rf_csv_dir)}),
            encoding="utf-8",
        )

        with patch("steps.transform_step.settings.data_dir", tmp_path):
            run("job-test", "2026-01", checkpoint_dir)

        manifest = json.loads(
            (checkpoint_dir / "transform_manifest.json").read_text(encoding="utf-8")
        )
        keywords = {t["keyword"].lower() for t in manifest["tables"]}
        assert "empresas" in keywords
        assert "estabelecimentos" in keywords
        assert "socios" in keywords
        assert "simples" in keywords
