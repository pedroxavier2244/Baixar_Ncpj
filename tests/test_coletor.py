"""
Guardas da lógica pura do coletor do Mac mini (`mac/coletor.py`).

O coletor não entra na imagem Docker e o que ele faz de verdade é rede, SSH e
rsync — nada disso é testável aqui. O que estes testes cobrem são as três
decisões que, se errarem, quebram em silêncio:

- a janela de publicação (mês recém-publicado não deve ser baixado);
- a leitura do JSON que vem da VPS, que chega junto com linhas de log;
- o `docker exec` montado com `-w /app` e PYTHONPATH — sem eles o import de
  `config` falha dentro do container.

E a tabela de decisão por status do job, que é a única coisa que impede o
coletor de disputar um mês com o worker.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

import pytest

RAIZ = Path(__file__).resolve().parent.parent


def _coletor():
    """Carrega mac/coletor.py pelo caminho — a pasta mac/ não é um pacote."""
    import sys
    if "coletor" in sys.modules:
        return sys.modules["coletor"]
    spec = importlib.util.spec_from_file_location("coletor", RAIZ / "mac" / "coletor.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["coletor"] = mod
    spec.loader.exec_module(mod)
    return mod


def _items(horas_atras: float) -> list[dict]:
    quando = datetime.now(timezone.utc) - timedelta(hours=horas_atras)
    return [{"name": "Empresas0.zip", "size": 10, "modified": format_datetime(quando)}]


# ── Janela de publicação ──────────────────────────────────────────────────────

def test_idade_recente():
    c = _coletor()
    assert c.idade_da_publicacao(_items(0.5)) == pytest.approx(0.5, abs=0.05)


def test_idade_antiga():
    c = _coletor()
    assert c.idade_da_publicacao(_items(30)) == pytest.approx(30, abs=0.05)


def test_idade_usa_o_arquivo_mais_recente():
    """A publicação de setembro levou 10 min entre o primeiro e o último arquivo."""
    c = _coletor()
    itens = _items(50) + _items(1)
    assert c.idade_da_publicacao(itens) == pytest.approx(1, abs=0.05)


def test_idade_sem_data_devolve_none():
    c = _coletor()
    assert c.idade_da_publicacao([{"name": "x.zip", "modified": ""}]) is None
    assert c.idade_da_publicacao([{"name": "x.zip"}]) is None


def test_idade_ignora_data_ilegivel():
    c = _coletor()
    itens = [{"name": "a.zip", "modified": "ontem de tarde"}] + _items(5)
    assert c.idade_da_publicacao(itens) == pytest.approx(5, abs=0.05)


def test_publicacao_recente_encerra_sem_baixar():
    c = _coletor()
    with patch.object(c.ej, "detect_wanted", return_value=("2026-10", _items(0.5))), \
         patch.object(c, "decidir_pelo_status") as status, \
         patch.object(c, "baixar") as baixar:
        with pytest.raises(c.NadaAFazer, match="publicacao"):
            c.coletar()
        status.assert_not_called()
        baixar.assert_not_called()


# ── Leitura do JSON que vem da VPS ────────────────────────────────────────────

def test_json_puro():
    c = _coletor()
    assert c._json_da_saida('{\n  "status": "PENDING"\n}') == {"status": "PENDING"}


def test_json_depois_de_linhas_de_log():
    """O logger do projeto escreve em stdout junto com o resultado."""
    c = _coletor()
    saida = (
        "2026-10-01 22:00:01 [INFO] enqueue_job: checando\n"
        '{\n  "status": "SUCCESS",\n  "job_id": "abc"\n}\n'
    )
    assert c._json_da_saida(saida)["job_id"] == "abc"


def test_json_null():
    c = _coletor()
    assert c._json_da_saida("null\n") is None
    assert c._json_da_saida("log qualquer\nnull\n") is None


def test_json_ausente_levanta():
    c = _coletor()
    with pytest.raises(c.ErroColetor, match="sem JSON"):
        c._json_da_saida("Error response from daemon: No such container\n")


# ── docker exec ───────────────────────────────────────────────────────────────

def test_cmd_container_tem_workdir_e_pythonpath():
    """Sem os dois, o Python põe a pasta do script no path e `config` não resolve."""
    c = _coletor()
    cmd = c._cmd_container("python", "enqueue_job.py", "--status", "--run-key", "2026-10")
    assert "-w /app" in cmd
    assert "PYTHONPATH=/app" in cmd
    assert cmd.index("-w") < cmd.index("enqueue_job.py")
    assert "enqueue_job.py --status --run-key 2026-10" in cmd


# ── Tabela de decisão por status ──────────────────────────────────────────────

def _com_status(c, job: dict | None):
    return patch.object(c, "_ssh", return_value=json.dumps(job))


@pytest.mark.parametrize("status", ["PENDING", "RUNNING", "FAILED"])
def test_status_sem_trabalho_encerra(status):
    """FAILED inclusive: o worker retenta sozinho até max_attempts."""
    c = _coletor()
    job = {"job_id": "j1", "status": status, "attempts": 1, "max_attempts": 3}
    with _com_status(c, job):
        with pytest.raises(c.NadaAFazer, match=status):
            c.decidir_pelo_status("2026-10")


def test_status_success_apaga_os_zips_e_encerra():
    c = _coletor()
    job = {"job_id": "j1", "status": "SUCCESS", "attempts": 1, "max_attempts": 3}
    with _com_status(c, job), patch.object(c, "apagar_zips_locais") as apagar:
        with pytest.raises(c.NadaAFazer, match="SUCCESS"):
            c.decidir_pelo_status("2026-10")
        apagar.assert_called_once_with("2026-10")


def test_status_dead_encerra_sem_apagar_nada():
    """DEAD pede intervenção: os ZIPs ficam para reenvio manual."""
    c = _coletor()
    job = {"job_id": "j1", "status": "DEAD", "attempts": 3, "max_attempts": 3,
           "last_error": "index_step timeout"}
    with _com_status(c, job), patch.object(c, "apagar_zips_locais") as apagar:
        with pytest.raises(c.NadaAFazer, match="DEAD"):
            c.decidir_pelo_status("2026-10")
        apagar.assert_not_called()


def test_sem_job_segue_em_frente():
    c = _coletor()
    with _com_status(c, None):
        assert c.decidir_pelo_status("2026-10") is None


def test_status_desconhecido_e_erro():
    c = _coletor()
    job = {"job_id": "j1", "status": "ZUMBI", "attempts": 0, "max_attempts": 3}
    with _com_status(c, job):
        with pytest.raises(c.ErroColetor, match="inesperado"):
            c.decidir_pelo_status("2026-10")


# ── Destino final na VPS ──────────────────────────────────────────────────────

def test_destino_divergente_aborta_o_envio():
    """
    Pasta do mês já existente e fora do manifest é o caso perigoso: pode ser
    entrega pela metade ou worker no meio do caminho.
    """
    c = _coletor()
    conf = {"existe": True, "ok": False, "problemas": ["falta Socios0.zip"],
            "conferidos": 1, "esperados": 2}
    with patch.object(c, "conferir_no_servidor", return_value=conf):
        assert c.estado_do_destino_final("2026-10", {"a": 1}) == "divergente"


def test_destino_completo_pula_o_envio():
    """Execução anterior entregou tudo e morreu antes de criar o job."""
    c = _coletor()
    conf = {"existe": True, "ok": True, "problemas": [], "conferidos": 2, "esperados": 2}
    with patch.object(c, "conferir_no_servidor", return_value=conf):
        assert c.estado_do_destino_final("2026-10", {"a": 1}) == "completo"


def test_destino_ausente_e_o_caminho_normal():
    c = _coletor()
    conf = {"existe": False, "ok": False, "problemas": ["falta a"],
            "conferidos": 0, "esperados": 1}
    with patch.object(c, "conferir_no_servidor", return_value=conf):
        assert c.estado_do_destino_final("2026-10", {"a": 1}) == "ausente"


# ── Trava ─────────────────────────────────────────────────────────────────────

def test_trava_impede_segunda_execucao(tmp_path):
    c = _coletor()
    with patch.object(c, "LOCK_PATH", tmp_path / "coletor.lock"):
        with c.trava():
            with pytest.raises(c.ErroColetor, match="em andamento"):
                with c.trava():
                    pass


def test_trava_liberada_ao_sair(tmp_path):
    c = _coletor()
    with patch.object(c, "LOCK_PATH", tmp_path / "coletor.lock"):
        with c.trava():
            pass
        with c.trava():  # não deve levantar
            pass


# ── Limpeza local ─────────────────────────────────────────────────────────────

def test_apagar_zips_locais_so_mexe_em_zip(tmp_path):
    c = _coletor()
    d = tmp_path / "data" / "2026-10"
    d.mkdir(parents=True)
    (d / "Empresas0.zip").write_bytes(b"x" * 10)
    (d / "Empresas1.zip.part").write_bytes(b"x" * 5)
    (d / "nao_mexer.txt").write_text("importante")
    with patch.object(c.settings, "data_dir", tmp_path / "data"):
        c.apagar_zips_locais("2026-10")
    assert not (d / "Empresas0.zip").exists()
    assert not (d / "Empresas1.zip.part").exists()
    assert (d / "nao_mexer.txt").exists()


def test_apagar_zips_locais_aceita_pasta_ausente(tmp_path):
    c = _coletor()
    with patch.object(c.settings, "data_dir", tmp_path / "data"):
        c.apagar_zips_locais("2026-10")  # não deve levantar


# ── Modo ensaio ───────────────────────────────────────────────────────────────
# O ensaio é rodado de propósito com um mês que já está SUCCESS — é o único
# jeito de exercitar o caminho inteiro sem risco. Se o passo 3 encerrasse nesse
# status, o ensaio nunca baixaria nada, e ainda apagaria os ZIPs locais.

@pytest.mark.parametrize("status", ["SUCCESS", "PENDING", "RUNNING", "FAILED", "DEAD"])
def test_ensaio_nao_encerra_por_status_nenhum(status):
    c = _coletor()
    job = {"job_id": "j1", "status": status, "attempts": 1, "max_attempts": 3}
    with _com_status(c, job), patch.object(c, "apagar_zips_locais") as apagar:
        assert c.decidir_pelo_status("2026-09", ensaio=True) is None
        apagar.assert_not_called()


def test_ensaio_envia_confere_e_para_antes_de_renomear():
    c = _coletor()
    conf_ok = {"existe": True, "ok": True, "problemas": [], "conferidos": 37, "esperados": 37}
    with patch.object(c.ej, "detect_wanted", return_value=("2026-09", _items(48))), \
         patch.object(c, "decidir_pelo_status"), \
         patch.object(c, "baixar"), \
         patch.object(c, "conferir_local", return_value={"Empresas0.zip": 10}), \
         patch.object(c, "enviar") as enviar, \
         patch.object(c, "conferir_no_servidor", return_value=conf_ok), \
         patch.object(c, "estado_do_destino_final") as destino, \
         patch.object(c, "renomear_no_servidor") as renomear, \
         patch.object(c, "entregar_manifest") as manifest, \
         patch.object(c, "criar_job") as criar:
        with pytest.raises(c.NadaAFazer, match="ensaio concluido"):
            c.coletar(ensaio=True)

        enviar.assert_called_once()
        # `.incoming` é o único destino que o ensaio toca
        assert c.conferir_no_servidor.call_args[0][0].endswith("2026-09.incoming")
        # e nada além disso
        destino.assert_not_called()
        renomear.assert_not_called()
        manifest.assert_not_called()
        criar.assert_not_called()


def test_ensaio_falha_se_o_servidor_nao_bater():
    c = _coletor()
    conf_ruim = {"existe": True, "ok": False, "problemas": ["falta Socios0.zip"],
                 "conferidos": 36, "esperados": 37}
    with patch.object(c.ej, "detect_wanted", return_value=("2026-09", _items(48))), \
         patch.object(c, "decidir_pelo_status"), \
         patch.object(c, "baixar"), \
         patch.object(c, "conferir_local", return_value={"Empresas0.zip": 10}), \
         patch.object(c, "enviar"), \
         patch.object(c, "conferir_no_servidor", return_value=conf_ruim), \
         patch.object(c, "renomear_no_servidor") as renomear:
        with pytest.raises(c.ErroColetor, match="conferencia no servidor"):
            c.coletar(ensaio=True)
        renomear.assert_not_called()


def test_execucao_normal_renomeia_e_cria_o_job():
    c = _coletor()
    conf_ok = {"existe": True, "ok": True, "problemas": [], "conferidos": 37, "esperados": 37}
    with patch.object(c.ej, "detect_wanted", return_value=("2026-10", _items(48))), \
         patch.object(c, "decidir_pelo_status"), \
         patch.object(c, "baixar"), \
         patch.object(c, "conferir_local", return_value={"Empresas0.zip": 10}), \
         patch.object(c, "enviar") as enviar, \
         patch.object(c, "conferir_no_servidor", return_value=conf_ok), \
         patch.object(c, "estado_do_destino_final", return_value="ausente"), \
         patch.object(c, "renomear_no_servidor") as renomear, \
         patch.object(c, "entregar_manifest") as manifest, \
         patch.object(c, "criar_job") as criar:
        c.coletar()
        enviar.assert_called_once()
        renomear.assert_called_once_with("2026-10")
        manifest.assert_called_once_with("2026-10")
        criar.assert_called_once_with("2026-10")


def test_destino_completo_pula_envio_mas_cria_o_job():
    """Entrega anterior completa que morreu antes de criar o job: só falta o job."""
    c = _coletor()
    with patch.object(c.ej, "detect_wanted", return_value=("2026-10", _items(48))), \
         patch.object(c, "decidir_pelo_status"), \
         patch.object(c, "baixar"), \
         patch.object(c, "conferir_local", return_value={"Empresas0.zip": 10}), \
         patch.object(c, "enviar") as enviar, \
         patch.object(c, "estado_do_destino_final", return_value="completo"), \
         patch.object(c, "renomear_no_servidor") as renomear, \
         patch.object(c, "entregar_manifest"), \
         patch.object(c, "criar_job") as criar:
        c.coletar()
        enviar.assert_not_called()
        renomear.assert_not_called()
        criar.assert_called_once_with("2026-10")


def test_destino_divergente_nao_cria_job():
    c = _coletor()
    with patch.object(c.ej, "detect_wanted", return_value=("2026-10", _items(48))), \
         patch.object(c, "decidir_pelo_status"), \
         patch.object(c, "baixar"), \
         patch.object(c, "conferir_local", return_value={"Empresas0.zip": 10}), \
         patch.object(c, "enviar") as enviar, \
         patch.object(c, "estado_do_destino_final", return_value="divergente"), \
         patch.object(c, "criar_job") as criar:
        with pytest.raises(c.ErroColetor, match="nao bate com o manifest"):
            c.coletar()
        enviar.assert_not_called()
        criar.assert_not_called()


# ── Batimento ─────────────────────────────────────────────────────────────────
# O batimento é a única defesa contra a falha que não produz erro nenhum: Mac
# mini desligado, sem rede ou com o daemon parado não geram log em lugar algum.

def test_batimento_gravado_no_caminho_feliz():
    c = _coletor()
    with patch.object(c, "_ssh", return_value="") as ssh:
        c.gravar_batimento("ok", "entregue", "2026-10 entregue", "2026-10", 31.4)
        remoto, = ssh.call_args[0]
        payload = json.loads(ssh.call_args[1]["entrada"])

    assert payload["resultado"] == "ok"
    assert payload["acao"] == "entregue"
    assert payload["run_key"] == "2026-10"
    assert payload["duracao_min"] == 31.4
    # tmp + mv: o vigia nunca pode ler um JSON pela metade
    assert ".tmp" in remoto and "mv " in remoto
    # sem o chown o worker (uid 1001) não lê o que o root escreveu
    assert "chown 1001:1001" in remoto


def test_batimento_registra_falha():
    """Sem isto o vigia levaria 48h para notar o que ele nota em 1h."""
    c = _coletor()
    with patch.object(c, "_ssh", return_value="") as ssh:
        c.gravar_batimento("falha", "erro", "download_step falhou", "2026-10", 3.2)
        payload = json.loads(ssh.call_args[1]["entrada"])
    assert payload["resultado"] == "falha"
    assert payload["detalhe"] == "download_step falhou"


def test_batimento_trunca_detalhe_longo():
    c = _coletor()
    with patch.object(c, "_ssh", return_value="") as ssh:
        c.gravar_batimento("falha", "erro", "x" * 5000, "2026-10", 1.0)
        payload = json.loads(ssh.call_args[1]["entrada"])
    assert len(payload["detalhe"]) == 500


def test_batimento_que_falha_nao_derruba_a_execucao():
    """O resultado real já está no log; um batimento perdido não pode mascará-lo."""
    c = _coletor()
    with patch.object(c, "_ssh", side_effect=c.ErroColetor("ssh caiu")):
        c.gravar_batimento("ok", "nada_a_fazer", "tudo certo", "2026-09", 0.1)


def test_batimento_tem_fuso():
    """O vigia compara com o agora dele; data sem fuso daria diferença de horas."""
    c = _coletor()
    with patch.object(c, "_ssh", return_value="") as ssh:
        c.gravar_batimento("ok", "nada_a_fazer", "ok", "2026-09", 0.1)
        ts = json.loads(ssh.call_args[1]["entrada"])["ts"]
    from datetime import datetime
    assert datetime.fromisoformat(ts).tzinfo is not None


def test_coletar_publica_o_run_key_no_contexto():
    """O encerramento normal sai por NadaAFazer; sem o ctx o batimento não teria mês."""
    c = _coletor()
    ctx = {}
    with patch.object(c.ej, "detect_wanted", return_value=("2026-10", _items(48))), \
         patch.object(c, "decidir_pelo_status", side_effect=c.NadaAFazer("ja esta SUCCESS")):
        with pytest.raises(c.NadaAFazer):
            c.coletar(ctx=ctx)
    assert ctx["run_key"] == "2026-10"
