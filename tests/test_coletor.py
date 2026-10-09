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
         patch.object(c, "avisar") as avisar, \
         patch.object(c, "criar_job") as criar:
        c.coletar()
        enviar.assert_called_once()
        renomear.assert_called_once_with("2026-10")
        manifest.assert_called_once_with("2026-10")
        criar.assert_called_once_with("2026-10")

    # três avisos de progresso: mês novo, download pronto, carga entregue
    textos = [ch.args[0] for ch in avisar.call_args_list]
    assert len(textos) == 3
    assert "A Receita publicou 2026-10" in textos[0]
    assert "Download de 2026-10 concluido" in textos[1]
    assert "2026-10 entregue" in textos[2]


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
         patch.object(c, "avisar"), \
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


# ── SSH: nova tentativa só quando a conexão nem chegou a abrir ────────────────
# Em 03/10/2026 um handshake travou por ~40s e derrubou a execução do dia; 2s
# depois o mesmo Mac conectou normal. Mas repetir às cegas é perigoso: se o
# comando chegou a rodar (renomear, --files-ready), rodar de novo duplica efeito.

def _cp(rc: int, stderr: str = "", stdout: str = ""):
    import subprocess
    return subprocess.CompletedProcess(["ssh"], rc, stdout=stdout, stderr=stderr)


_TIMEOUT_HANDSHAKE = "Connection to 5.189.163.33 port 22 timed out"


def test_ssh_tenta_de_novo_quando_a_conexao_nao_abre():
    c = _coletor()
    respostas = [_cp(255, _TIMEOUT_HANDSHAKE), _cp(0, stdout="ok\n")]
    with patch.object(c.subprocess, "run", side_effect=respostas) as run, \
         patch.object(c.time, "sleep") as dorme:
        assert c._ssh("true") == "ok\n"
    assert run.call_count == 2
    dorme.assert_called_once()


def test_ssh_reenvia_o_stdin_na_nova_tentativa():
    """O batimento e o aviso vão pelo stdin; a segunda tentativa precisa dele inteiro."""
    c = _coletor()
    respostas = [_cp(255, "kex_exchange_identification: Connection closed by remote host"),
                 _cp(0)]
    with patch.object(c.subprocess, "run", side_effect=respostas) as run, \
         patch.object(c.time, "sleep"):
        c._ssh("cat > x", entrada='{"a": 1}')
    assert [ch.kwargs["input"] for ch in run.call_args_list] == ['{"a": 1}'] * 2


@pytest.mark.parametrize("stderr", [
    _TIMEOUT_HANDSHAKE,
    "ssh: connect to host 5.189.163.33 port 22: Operation timed out",
    "ssh: connect to host 5.189.163.33 port 22: Connection refused",
    "ssh: connect to host 5.189.163.33 port 22: No route to host",
    "ssh: Could not resolve hostname cnpj-vps: nodename nor servname provided",
    "kex_exchange_identification: read: Connection reset by peer",
    "Connection closed by 5.189.163.33 port 22",
    "Connection timed out during banner exchange",
])
def test_ssh_reconhece_falha_de_conexao(stderr):
    c = _coletor()
    assert c._falha_de_conexao(_cp(255, stderr))


@pytest.mark.parametrize("cp", [
    # caiu no meio da sessão: o comando pode ter rodado
    _cp(255, "Connection to 5.189.163.33 closed by remote host."),
    _cp(255, "Timeout, server 5.189.163.33 not responding."),
    # o próprio comando remoto falhou — repetir não muda nada
    _cp(1, "mv: cannot stat '/x': No such file or directory"),
    _cp(255, ""),
])
def test_ssh_nao_confunde_outras_falhas_com_conexao(cp):
    c = _coletor()
    assert not c._falha_de_conexao(cp)


def test_ssh_nao_repete_queda_no_meio_da_sessao():
    """Comando que pode ter rodado (renomear, criar job) não é executado duas vezes."""
    c = _coletor()
    with patch.object(c.subprocess, "run",
                      return_value=_cp(255, "Connection to x closed by remote host.")) as run, \
         patch.object(c.time, "sleep"):
        with pytest.raises(c.ErroColetor, match="closed by remote host"):
            c._ssh("criar job")
    assert run.call_count == 1


def test_ssh_desiste_depois_das_tentativas():
    c = _coletor()
    with patch.object(c.subprocess, "run", return_value=_cp(255, _TIMEOUT_HANDSHAKE)) as run, \
         patch.object(c.time, "sleep"):
        with pytest.raises(c.ErroColetor, match="timed out") as erro:
            c._ssh("true")
    assert run.call_count == c.TENTATIVAS_SSH
    # a mensagem que vai para o batimento diz que já houve insistência
    assert f"{c.TENTATIVAS_SSH} tentativas" in str(erro.value)


def test_ssh_passa_connect_timeout():
    """Sem prazo explícito, o tempo de desistir fica a cargo do ~/.ssh/config do Mac."""
    c = _coletor()
    with patch.object(c.subprocess, "run", return_value=_cp(0)) as run:
        c._ssh("true")
    argv = run.call_args[0][0]
    assert any(a.startswith("ConnectTimeout=") for a in argv)


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


# ── Avisos de progresso ───────────────────────────────────────────────────────

def test_avisar_nao_envia_sob_pytest():
    """
    Trava dura contra o erro de 21/09/2026: dois testes que chamam coletar()
    esqueceram de mockar `avisar` e a suite mandou 8 mensagens para uma pessoa
    de verdade. Mockar em cada teste é disciplina; isto é garantia.
    """
    c = _coletor()
    with patch.object(c, "_ssh") as ssh:
        c.avisar("mensagem que nao pode sair")
        ssh.assert_not_called()


def test_avisar_respeita_a_chave_de_desligar(monkeypatch):
    c = _coletor()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with patch.object(c, "AVISAR", False), patch.object(c, "_ssh") as ssh:
        c.avisar("nao envia")
        ssh.assert_not_called()


def test_avisar_manda_o_json_pelo_stdin(monkeypatch):
    """A mensagem tem acento e quebra de linha; na linha de comando isso quebra."""
    c = _coletor()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with patch.object(c, "_ssh", return_value="\n200") as ssh:
        c.avisar("🆕 *Publicou*\nlinha dois")
        remoto, = ssh.call_args[0]
        corpo = json.loads(ssh.call_args[1]["entrada"])
    assert "--data @-" in remoto
    assert corpo["to"] == c.DEST_WHATSAPP
    assert corpo["text"] == "🆕 *Publicou*\nlinha dois"


def test_avisar_reclama_quando_a_bridge_recusa(monkeypatch, caplog):
    """O /health da bridge diz 'conectado' mesmo com o socket caido."""
    c = _coletor()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with patch.object(c, "_ssh", return_value='{"erro":"Connection Closed"}\n500'):
        with caplog.at_level("WARNING"):
            c.avisar("tentativa")
    assert "NAO saiu" in caplog.text and "500" in caplog.text


def test_avisar_que_falha_nao_derruba_a_execucao(monkeypatch):
    """Aviso é conforto; a carga não pode parar porque o WhatsApp caiu."""
    c = _coletor()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with patch.object(c, "_ssh", side_effect=c.ErroColetor("ssh caiu")):
        c.avisar("tentativa")


def test_ensaio_nao_avisa_ninguem():
    """Ensaio é teste manual: 'A Receita publicou' seria alarme falso."""
    c = _coletor()
    conf_ok = {"existe": True, "ok": True, "problemas": [], "conferidos": 37, "esperados": 37}
    with patch.object(c.ej, "detect_wanted", return_value=("2026-09", _items(48))), \
         patch.object(c, "decidir_pelo_status"), \
         patch.object(c, "baixar"), \
         patch.object(c, "conferir_local", return_value={"Empresas0.zip": 10}), \
         patch.object(c, "enviar"), \
         patch.object(c, "conferir_no_servidor", return_value=conf_ok), \
         patch.object(c, "avisar") as avisar:
        with pytest.raises(c.NadaAFazer, match="ensaio concluido"):
            c.coletar(ensaio=True)
    avisar.assert_not_called()


def test_dia_sem_novidade_nao_avisa():
    """
    ~29 dias por mês o coletor encerra em 6s. Aviso diário de 'nada novo'
    treinaria quem lê a ignorar a mensagem que importa.
    """
    c = _coletor()
    with patch.object(c.ej, "detect_wanted", return_value=("2026-09", _items(48))), \
         patch.object(c, "decidir_pelo_status", side_effect=c.NadaAFazer("ja esta SUCCESS")), \
         patch.object(c, "avisar") as avisar:
        with pytest.raises(c.NadaAFazer):
            c.coletar()
    avisar.assert_not_called()


# ── Aba Logs do CRM ───────────────────────────────────────────────────────────
# O batimento é alarme do vigia; isto aqui é a linha que a conta dev lê no CRM.
# São públicos diferentes, e o envio passa pelo MESMO SSH do batimento de propósito:
# assim o AUTOMACAO_LOG_TOKEN continua só no .env da VPS e não precisa ser copiado
# para o Mac mini nem para o plist do LaunchDaemon.

def test_publica_a_rodada_no_crm_com_o_contrato_da_rota():
    c = _coletor()
    with patch.object(c, "_ssh", return_value="200") as ssh:
        c.publicar_no_crm("ok", "nada_a_fazer", "2026-09 ja esta SUCCESS na VPS",
                          "2026-09", 0.3, "2026-10-02T01:00:00+00:00")
        remoto, = ssh.call_args[0]
        payload = json.loads(ssh.call_args[1]["entrada"])

    # o id tem de ser o do CATALOGO do CRM, senao a rota responde 400
    assert payload["automacao"] == "receita-cnpj"
    assert payload["origem"] == "mac-mini"
    assert payload["status"] == "ok"
    assert payload["inicio"] == "2026-10-02T01:00:00+00:00"
    assert "nada_a_fazer" in payload["resumo"]
    assert payload["erro"] is None
    assert payload["detalhe"]["run_key"] == "2026-09"
    assert payload["detalhe"]["duracao_min"] == 0.3


def test_token_e_lido_na_vps_e_nao_viaja_como_argumento():
    """Token em argumento de comando aparece no `ps` de quem estiver na maquina."""
    c = _coletor()
    with patch.object(c, "_ssh", return_value="200") as ssh:
        c.publicar_no_crm("ok", "nada_a_fazer", "x", "2026-09", 0.1, "2026-10-02T01:00:00+00:00")
        remoto, = ssh.call_args[0]

    assert "AUTOMACAO_LOG_TOKEN" in remoto and "/opt/apps/mb-crm/.env" in remoto
    assert "$TOKEN" in remoto           # sai por variavel do shell remoto
    assert "127.0.0.1:3002" in remoto   # o crm-api, nao o nginx


@pytest.mark.parametrize("linha_env", [
    'AUTOMACAO_LOG_TOKEN=abc123\n',
    'AUTOMACAO_LOG_TOKEN="abc123"\r\n',   # .env salvo no Windows, com aspas
    "AUTOMACAO_LOG_TOKEN='abc123'\n",
])
def test_comando_do_crm_roda_no_bash_e_extrai_o_token_limpo(tmp_path, linha_env):
    """
    Executa o comando remoto de verdade, com .env e curl falsos. De 02/10 a
    09/10/2026 o \\r\\n do `tr` era CR/LF de verdade na string e o bash da VPS
    morria com "unexpected EOF" — os asserts de substring acima não pegaram.
    """
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash ausente")

    env_file = tmp_path / ".env"
    env_file.write_text("OUTRA=1\n" + linha_env, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    # O curl falso grava os argumentos e o corpo, e responde como o crm-api.
    curl.write_text(
        '#!/bin/sh\n'
        f'printf "%s\\n" "$@" > {tmp_path}/args\n'
        f'cat > {tmp_path}/corpo\n'
        'printf 201\n'
    )
    curl.chmod(0o755)

    c = _coletor()
    with patch.object(c, "_ssh", return_value="201") as ssh:
        c.publicar_no_crm("ok", "nada_a_fazer", "x", "2026-09", 0.1, "2026-10-02T01:00:00+00:00")
        remoto, = ssh.call_args[0]
        entrada = ssh.call_args[1]["entrada"]

    remoto = remoto.replace("/opt/apps/mb-crm/.env", str(env_file))
    cp = subprocess.run(
        [bash, "-c", remoto], input=entrada, capture_output=True, text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"}, timeout=10,
    )

    assert cp.returncode == 0, cp.stderr
    assert cp.stdout == "201"
    args = (tmp_path / "args").read_text().splitlines()
    assert "x-automacao-token: abc123" in args
    assert json.loads((tmp_path / "corpo").read_text())["automacao"] == "receita-cnpj"


def test_rodada_que_falhou_vai_como_erro():
    c = _coletor()
    with patch.object(c, "_ssh", return_value="200") as ssh:
        c.publicar_no_crm("falha", "erro", "download_step falhou", "2026-10", 3.2,
                          "2026-10-02T01:00:00+00:00")
        payload = json.loads(ssh.call_args[1]["entrada"])
    assert payload["status"] == "erro"
    assert payload["erro"] == "download_step falhou"


def test_crm_fora_do_ar_nao_derruba_a_execucao(caplog):
    """Perder a linha do log e melhor que perder a rodada -- mesma regra do batimento."""
    c = _coletor()
    with patch.object(c, "_ssh", side_effect=c.ErroColetor("ssh falhou (rc=255)")):
        c.publicar_no_crm("ok", "entregue", "2026-10 entregue", "2026-10", 31.4,
                          "2026-10-02T01:00:00+00:00")
    assert "nao consegui publicar" in caplog.text


def test_crm_que_recusa_aparece_no_log(caplog):
    c = _coletor()
    with patch.object(c, "_ssh", return_value="401"):
        c.publicar_no_crm("ok", "entregue", "x", "2026-10", 1.0, "2026-10-02T01:00:00+00:00")
    assert "HTTP 401" in caplog.text
