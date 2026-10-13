"""A resposta da Receita que NÃO é a listagem — e a mensagem que sobra disso.

Em 12/10/2026 o coletor morreu no primeiro passo com:

    RetryError[<Future at 0x1057809e0 state=finished raised ParseError>]

Essa frase foi para o WhatsApp e para a aba Logs do CRM. Ela não diz o host, nem o status
HTTP, nem o que veio no lugar do XML — e o corpo era descartado, então no dia seguinte,
com o servidor já bom, não havia o que investigar.

Isso pesa porque a Receita publica a base no MEIO DO MÊS (a de setembro entrou em 16/09):
uma página de erro na noite da publicação atrasa o mês inteiro com uma mensagem inútil.

O caso que me surpreendeu ao escrever isto: uma página de erro `<html>...</html>` é XML
BEM-FORMADO. Ela atravessava o parser sem erro nenhum e viraria "nenhum mês publicado" em
silêncio — pior que falhar.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path

import httpx
import pytest

import enqueue_job as ej

URL = "https://arquivos.receitafederal.gov.br/public.php/dav/files/ABC/"
REQ = httpx.Request("PROPFIND", URL)


def _resp(status: int, texto: str, tipo: str = "application/xml") -> httpx.Response:
    return httpx.Response(status, headers={"content-type": tipo}, text=texto, request=REQ)


@pytest.fixture(autouse=True)
def amostras_em_tmp(tmp_path, monkeypatch):
    """As amostras vão para o tmp do teste, nunca para ~/cnpj/logs."""
    monkeypatch.setattr(ej, "AMOSTRA_DIR", tmp_path)
    return tmp_path


LISTAGEM_BOA = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"></d:multistatus>'


def test_listagem_de_verdade_passa():
    assert ej._listagem_de(_resp(207, LISTAGEM_BOA), URL).tag == "{DAV:}multistatus"


def test_pagina_de_erro_bem_formada_nao_passa_por_listagem_vazia():
    """O caso perigoso: XML válido, conteúdo errado. Sem esta guarda, o coletor diria
    'nenhum mês publicado' e ninguém saberia que o servidor respondeu 502."""
    html = "<html><head><title>502 Bad Gateway</title></head><body><h1>502</h1></body></html>"
    with pytest.raises(ej.RespostaNaoXML) as e:
        ej._listagem_de(_resp(502, html, "text/html"), URL)
    assert "raiz do XML é <html>" in str(e.value)


@pytest.mark.parametrize("status,corpo,tipo", [
    (207, '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response><d:href>/x', "application/xml"),
    (503, '{"error":"service unavailable"}', "application/json"),
    (200, "", "text/plain"),
])
def test_corpo_que_nao_e_xml_vira_erro_que_diz_o_que_veio(status, corpo, tipo):
    with pytest.raises(ej.RespostaNaoXML) as e:
        ej._listagem_de(_resp(status, corpo, tipo), URL)
    msg = str(e.value)
    assert "arquivos.receitafederal.gov.br" in msg, "a mensagem tem de dizer QUAL servidor"
    assert f"HTTP {status}" in msg, "e o status que ele devolveu"
    assert tipo in msg, "e o content-type, que distingue pagina de erro de XML truncado"


def test_a_mensagem_mostra_o_comeco_do_corpo():
    corpo = "ERRO INTERNO DO PORTAL " + "x" * 500
    with pytest.raises(ej.RespostaNaoXML) as e:
        ej._listagem_de(_resp(500, corpo, "text/plain"), URL)
    msg = str(e.value)
    assert "ERRO INTERNO DO PORTAL" in msg
    assert len(msg) < 700, "mensagem vai para o WhatsApp: tem de caber na tela"


def test_corpo_inteiro_fica_em_disco_para_investigar_depois(amostras_em_tmp):
    corpo = "<html>" + "y" * 3000 + "</html>"
    with pytest.raises(ej.RespostaNaoXML):
        ej._listagem_de(_resp(502, corpo, "text/html"), URL)
    arquivos = list(amostras_em_tmp.glob("propfind-nao-xml-*.txt"))
    assert len(arquivos) == 1
    conteudo = arquivos[0].read_text(encoding="utf-8")
    assert URL in conteudo and "status: 502" in conteudo and "y" * 3000 in conteudo


def test_tres_tentativas_no_mesmo_segundo_geram_tres_amostras(amostras_em_tmp):
    """O tenacity tenta 3x com espera curta: com precisão de segundo no nome, a terceira
    sobrescrevia as outras duas — justo quando ter as três mostra se o servidor variou."""
    for _ in range(3):
        with pytest.raises(ej.RespostaNaoXML):
            ej._listagem_de(_resp(502, "nao e xml", "text/html"), URL)
    assert len(list(amostras_em_tmp.glob("propfind-nao-xml-*.txt"))) == 3


def test_falha_ao_gravar_a_amostra_nao_engole_o_erro_de_verdade(monkeypatch):
    """Disco cheio ou pasta sem permissão não pode trocar a mensagem útil por um
    IOError sem relação com o problema."""
    monkeypatch.setattr(ej, "AMOSTRA_DIR", Path("/nao/existe/mesmo"))
    with pytest.raises(ej.RespostaNaoXML) as e:
        ej._listagem_de(_resp(503, "{}", "application/json"), URL)
    assert "HTTP 503" in str(e.value)
    assert "Corpo inteiro em" not in str(e.value), "sem amostra, não promete amostra"


def test_e_retentavel_pelo_tenacity():
    """Resposta não-XML quase sempre é soluço do servidor: as 3 tentativas valem a pena.
    Se um dia alguém trocar a classe base, o retry do propfind_listing para de cobrir."""
    assert issubclass(ej.RespostaNaoXML, Exception)
    assert not issubclass(ej.RespostaNaoXML, (KeyboardInterrupt, SystemExit))
