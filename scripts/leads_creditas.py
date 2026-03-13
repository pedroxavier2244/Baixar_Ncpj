"""
Exporta leads filtrados pelos critérios Creditas para CSV.

Filtros ativos:
  - UF: SP, MG, PR
  - Situacao cadastral: 02 (ativa)
  - Faixa etaria dos socios: 4 (31-40 anos) e 5 (41-50 anos)

Uso:
  python scripts/leads_creditas.py
  python scripts/leads_creditas.py --uf SP MG PR --output /tmp/leads.csv
"""
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import psycopg

from config import settings

SQL = """
SELECT
    mv.cnpj_completo,
    mv.razao_social,
    mv.nome_fantasia,
    mv.uf,
    mv.municipio_descricao,
    COALESCE(mv.ddd1, '') || COALESCE(mv.telefone1, '') AS telefone,
    mv.correio_eletronico,
    s.nome_socio,
    s.faixa_etaria,
    s.qualificacao_socio
FROM cnpj_serving.mv_cnpj_full mv
JOIN cnpj.rf_socios s ON s.cnpj_basico = mv.cnpj_basico
WHERE mv.uf = ANY(%(ufs)s)
  AND mv.situacao_cadastral = '02'
  AND s.faixa_etaria IN ('4', '5')
ORDER BY mv.uf, mv.municipio_descricao
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--uf", nargs="+", default=["SP", "MG", "PR"])
    parser.add_argument("--output", default="/tmp/leads_creditas.csv")
    args = parser.parse_args()

    output = Path(args.output)
    print(f"Conectando ao banco...")

    with psycopg.connect(settings.postgres_url) as conn:
        print(f"Consultando UFs: {args.uf} (pode demorar alguns minutos)...")
        cur = conn.execute(SQL, {"ufs": args.uf})
        rows = cur.fetchall()
        cols = [d.name for d in cur.description]

    print(f"Encontrados: {len(rows):,} registros")

    with open(output, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(cols)
        w.writerows(rows)

    print(f"Salvo em: {output}")


if __name__ == "__main__":
    main()
