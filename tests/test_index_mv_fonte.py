"""O refresh é o ato destrutivo — o que roda ANTES dele (14/10/2026).

A carga mensal de 14/10 deixou a API `/cnpj/{cnpj}` respondendo "não encontrado"
para qualquer CNPJ por mais de 12 horas, em pleno expediente da receptiva. A
sequência, lida no log do `cnpj_worker` e no `pg_matviews` da VPS:

  06/10  reinício sujo do Postgres esvazia as rf_* (são UNLOGGED). A MV não é
         UNLOGGED: ela sobrevive, e a API segue respondendo POR ELA. Ninguém vê.
  14/10  carga mensal: as tabelas novas carregam certo (70,5M / 73,8M / 50,8M),
         e o swap `ALTER TABLE ... RENAME` manda as VAZIAS para _old.
  03:34  a MV existe e está populada -> REFRESH CONCURRENTLY. Mas a definição da
         MV segue o OID, e o rename a arrastou para as _old vazias: o refresh
         apaga a última cópia do dado.
  06:20  "refresh complete — counting rows..."
  06:42  "mv_cnpj_full: 0 rows" -> passo FALHA. A guarda funcionou e não serviu
         de nada: o estrago foi o refresh, 3 horas antes.
  07:42, 08:47  mesma coisa, de novo.

Daí as duas regras que estes testes travam: não refrescar com a fonte vazia, e
não refrescar quando a definição aponta para as _old.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import steps.index_step as ix
from steps.base import StepStatus


class FakeConn:
    """Conexão que responde por trecho de SQL e guarda a ordem do que rodou."""

    def __init__(self, *, populada=True, definicao=None, fontes_com_dado=True,
                 nova_com_dado=True):
        self.sqls: list[str] = []
        self._populada = populada
        self._definicao = definicao
        self._fontes_com_dado = fontes_com_dado
        self._nova_com_dado = nova_com_dado

    # psycopg: conn.execute(sql, params) -> cursor com fetchone()
    def execute(self, sql, params=None):
        self.sqls.append(" ".join(str(sql).split()))
        cur = MagicMock()
        cur.fetchone.return_value = self._resposta(str(sql))
        return cur

    def _resposta(self, sql):
        if "ispopulated" in sql:
            return (True,) if self._populada else None
        if "definition" in sql and "pg_matviews" in sql:
            return (self._definicao,) if self._definicao else (None,)
        if "SELECT EXISTS" in sql:
            alvo_nova = ix._MV_NOVA in sql
            return (self._nova_com_dado,) if alvo_nova else (self._fontes_com_dado,)
        if "COUNT(*)" in sql:
            return (70_533_504,)
        if "information_schema.tables" in sql:
            return None  # sem _old para dropar
        return None

    def cursor(self):
        return MagicMock()

    def commit(self):
        pass

    def transaction(self):
        return MagicMock()

    def rodou(self, trecho):
        return [s for s in self.sqls if trecho in s]

    def posicao(self, trecho):
        for i, s in enumerate(self.sqls):
            if trecho in s:
                return i
        return -1


DEF_OLD = ("SELECT ... FROM cnpj.rf_estabelecimentos_old est "
           "JOIN cnpj.rf_empresas_old e ON e.cnpj_basico = est.cnpj_basico")
DEF_BOA = ("SELECT ... FROM cnpj.rf_estabelecimentos est "
           "JOIN cnpj.rf_empresas e ON e.cnpj_basico = est.cnpj_basico")


@pytest.fixture(autouse=True)
def sessao_sem_settings(monkeypatch):
    monkeypatch.setattr(ix, "_apply_session_settings", lambda conn: None)
    monkeypatch.setattr(ix, "_flush_redis_cache", lambda: None)
    monkeypatch.setattr(ix, "_choose_refresh_strategy", lambda: "concurrent")


def _rodar(conn, tmp_path):
    def conectar(*a, **k):
        ctx = MagicMock()
        ctx.__enter__ = lambda s: conn
        ctx.__exit__ = lambda s, *e: False
        return ctx
    import psycopg
    orig = psycopg.connect
    psycopg.connect = conectar
    try:
        return ix.run("job", "2026-10", tmp_path)
    finally:
        psycopg.connect = orig


# ── fonte vazia: não encostar na MV ──────────────────────────────────────────

def test_fonte_vazia_nao_refresca_e_preserva_a_mv(tmp_path):
    """O teste do incidente: com as fontes vazias, NADA de refresh."""
    conn = FakeConn(populada=True, definicao=DEF_BOA, fontes_com_dado=False)
    res = _rodar(conn, tmp_path)

    assert res.status is StepStatus.FAILED
    assert conn.rodou("REFRESH") == [], "refresh com fonte vazia apaga a MV"
    assert conn.rodou("DROP MATERIALIZED VIEW") == []
    assert "rf_estabelecimentos" in res.error, "a falha tem de dizer QUAL tabela"
    assert "preservada" in res.error


def test_fonte_vazia_nao_dropa_as_old(tmp_path):
    """As _old são o que resta: dropá-las fecha a porta de recuperar o dado."""
    conn = FakeConn(populada=True, definicao=DEF_BOA, fontes_com_dado=False)
    _rodar(conn, tmp_path)
    assert conn.rodou("DROP TABLE") == []


def test_fonte_vazia_usa_exists_nao_count(tmp_path):
    """COUNT(*) nesta base levou 22 minutos no log de 14/10 — a pergunta é só
    'tem dado?'."""
    conn = FakeConn(populada=True, definicao=DEF_BOA, fontes_com_dado=False)
    _rodar(conn, tmp_path)
    assert conn.rodou("SELECT EXISTS"), "a checagem de fonte tem de ser EXISTS"
    assert conn.rodou("COUNT(*)") == []


# ── definição presa nas _old ─────────────────────────────────────────────────

def test_definicao_nas_old_nao_refresca(tmp_path):
    """REFRESH aqui leria a tabela errada — foi exatamente o que aconteceu."""
    conn = FakeConn(populada=True, definicao=DEF_OLD)
    res = _rodar(conn, tmp_path)

    assert res.status is StepStatus.SUCCESS
    assert conn.rodou("REFRESH") == []
    assert conn.rodou(f"CREATE MATERIALIZED VIEW {ix._MV_NOVA}")


def test_rebuild_so_troca_depois_de_a_nova_estar_pronta(tmp_path):
    """A MV viva não pode sair do ar durante a construção: em 14/10 ela levou
    2h46, o que cairia dentro do expediente."""
    conn = FakeConn(populada=True, definicao=DEF_OLD)
    _rodar(conn, tmp_path)

    criou = conn.posicao(f"CREATE MATERIALIZED VIEW {ix._MV_NOVA}")
    # igualdade, não "contém": `... IF EXISTS {_MV}` é prefixo de `..._novo`
    dropou = next(i for i, s in enumerate(conn.sqls)
                  if s == f"DROP MATERIALIZED VIEW IF EXISTS {ix._MV}")
    renomeou = conn.posicao("RENAME TO mv_cnpj_full")
    assert criou >= 0 and renomeou > criou
    assert dropou > criou, "a viva só sai depois de a nova existir com dado"
    assert dropou < renomeou


def test_nova_vazia_nao_entra_no_lugar_da_viva(tmp_path):
    """Trocar por uma MV vazia é pior que não trocar."""
    conn = FakeConn(populada=True, definicao=DEF_OLD, nova_com_dado=False)
    res = _rodar(conn, tmp_path)

    assert res.status is StepStatus.FAILED
    assert conn.rodou("RENAME TO mv_cnpj_full") == []
    assert conn.rodou(f"DROP MATERIALIZED VIEW IF EXISTS {ix._MV_NOVA}")


def test_indices_da_nova_voltam_com_o_nome_definitivo(tmp_path):
    """O REFRESH CONCURRENTLY do mês seguinte EXIGE o unique index; se ele
    ficasse 'idx_mv_cnpj_completo_novo', a carga de dezembro falharia sem
    relação aparente com este rebuild."""
    conn = FakeConn(populada=True, definicao=DEF_OLD)
    _rodar(conn, tmp_path)

    for nome, _u, _c in ix._MV_INDEXES_SPEC:
        assert conn.rodou(f"ALTER INDEX {ix._V}.{nome}_novo RENAME TO {nome}"), nome


def test_nova_nasce_com_dado_e_com_o_unique_index(tmp_path):
    conn = FakeConn(populada=True, definicao=DEF_OLD)
    _rodar(conn, tmp_path)
    criacao = conn.rodou(f"CREATE MATERIALIZED VIEW {ix._MV_NOVA}")[0]
    assert "WITH NO DATA" not in criacao
    assert conn.rodou(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_cnpj_completo_novo")


# ── caminho normal: segue como era ───────────────────────────────────────────

def test_definicao_certa_e_fonte_com_dado_segue_refresh_concurrently(tmp_path):
    conn = FakeConn(populada=True, definicao=DEF_BOA)
    res = _rodar(conn, tmp_path)

    assert res.status is StepStatus.SUCCESS
    assert conn.rodou("REFRESH MATERIALIZED VIEW CONCURRENTLY")
    assert conn.rodou(f"CREATE MATERIALIZED VIEW {ix._MV_NOVA}") == [], \
        "sem motivo, não reconstrói: o rebuild custa ~3h"


def test_mv_ausente_recria_no_nome_definitivo(tmp_path):
    """Sem MV não há o que proteger — recria direto e refresca."""
    conn = FakeConn(populada=False, definicao=None)
    conn._populada = None
    res = _rodar(conn, tmp_path)
    assert res.status is StepStatus.SUCCESS
    assert conn.rodou(f"CREATE MATERIALIZED VIEW {ix._MV} ")
    assert conn.rodou("REFRESH MATERIALIZED VIEW cnpj_serving.mv_cnpj_full")


# ── o SELECT é um só nos dois caminhos ───────────────────────────────────────

def test_ddl_dos_dois_caminhos_sai_do_mesmo_select():
    """Duas cópias do SELECT divergem com o tempo, e a MV passa a ter colunas
    diferentes dependendo do caminho que a carga tomou."""
    assert ix._MV_SELECT in ix._ddl_mv(ix._MV, com_dado=False)
    assert ix._MV_SELECT in ix._ddl_mv(ix._MV_NOVA, com_dado=True)
    assert ix._ddl_mv(ix._MV, com_dado=False).rstrip().endswith("WITH NO DATA")


def test_fontes_obrigatorias_sao_as_do_join_nao_as_de_dominio():
    """rf_cnaes/rf_municipios estão com 0 linhas há meses e entram por LEFT
    JOIN — exigi-las aqui faria toda carga falhar sem necessidade."""
    assert ix._FONTES_OBRIGATORIAS == ["cnpj.rf_estabelecimentos", "cnpj.rf_empresas"]
    sel = " ".join(ix._MV_SELECT.split())
    for t in ix._FONTES_OBRIGATORIAS:
        assert f"JOIN {t}" in sel or f"FROM {t}" in sel
