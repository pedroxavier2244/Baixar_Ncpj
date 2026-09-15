"""
O Dockerfile copia uma lista explícita de módulos da raiz. Módulo novo que não
entra nessa lista some da imagem, e a falha só aparece em produção, no import.

Foi o que aconteceu em 15/09/2026 com `webdav.py`: os testes passavam local
(rodam da raiz do repo) e o container subia sem o módulo, em crash-loop.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# Código que vai para a imagem — se importar um módulo da raiz, ele precisa ser copiado.
PACOTES_DE_PRODUCAO = ("api", "steps", "db")


def _modulos_copiados() -> set[str]:
    """Nomes de módulos da raiz presentes nas linhas COPY do Dockerfile."""
    texto = (RAIZ / "Dockerfile").read_text(encoding="utf-8")
    # junta continuações de linha (COPY a.py b.py \\\n   c.py ./)
    texto = texto.replace("\\\n", " ")
    copiados: set[str] = set()
    for linha in texto.splitlines():
        if not linha.strip().startswith("COPY"):
            continue
        for token in re.findall(r"([\w./-]+\.py)", linha):
            copiados.add(Path(token).stem)
    return copiados


def _modulos_da_raiz() -> set[str]:
    return {p.stem for p in RAIZ.glob("*.py")}


def _imports_de(caminho: Path) -> set[str]:
    try:
        arvore = ast.parse(caminho.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    nomes: set[str] = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            for alias in no.names:
                nomes.add(alias.name.split(".")[0])
        elif isinstance(no, ast.ImportFrom):
            if no.level == 0 and no.module:
                nomes.add(no.module.split(".")[0])
    return nomes


def _arquivos_de_producao() -> list[Path]:
    arquivos = [RAIZ / f"{m}.py" for m in _modulos_copiados() if (RAIZ / f"{m}.py").exists()]
    for pacote in PACOTES_DE_PRODUCAO:
        arquivos.extend(sorted((RAIZ / pacote).rglob("*.py")))
    return arquivos


def test_todo_modulo_da_raiz_importado_em_producao_vai_para_a_imagem():
    copiados = _modulos_copiados()
    da_raiz = _modulos_da_raiz()

    faltando: dict[str, list[str]] = {}
    for arquivo in _arquivos_de_producao():
        for nome in _imports_de(arquivo):
            if nome in da_raiz and nome not in copiados:
                faltando.setdefault(nome, []).append(
                    str(arquivo.relative_to(RAIZ)).replace("\\", "/")
                )

    assert not faltando, (
        "módulo(s) da raiz importados pelo código de produção mas ausentes do "
        f"COPY do Dockerfile: { {k: sorted(v) for k, v in faltando.items()} }"
    )


def test_webdav_esta_no_dockerfile():
    """Guarda explícita: foi esse módulo que derrubou o container em 15/09/2026."""
    assert "webdav" in _modulos_copiados()
