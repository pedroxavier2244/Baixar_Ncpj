#!/usr/bin/env python3
"""
Coletor da Receita no Mac mini — baixa os ZIPs do mês e entrega na VPS.

Por que existe: desde 11/09/2026 `arquivos.receitafederal.gov.br` derruba toda
conexão HTTPS vinda do IP da VPS, que é de datacenter. Uma conexão brasileira
comum continua alcançando. Então o download sai daqui e o processamento
continua lá — ver `docs/HANDOFF-MAC-MINI.md`.

Este script nunca toca o Postgres e não guarda estado próprio: "esse mês já foi
processado?" é respondido pelo `job_queue` da VPS, por SSH, a cada execução. O
único estado local são os ZIPs, apagados apenas depois do job dar SUCCESS.

Ordem dos passos — para no primeiro erro:

    1. detecta o mês publicado na Receita
    2. recusa mês publicado há menos de IDADE_MINIMA_H horas (publicação em curso)
    3. consulta o status do job na VPS e decide se há trabalho
    4. baixa os ZIPs com o download_step de produção (resume via .part)
    5. confere tamanho contra o manifest e abre cada ZIP
    6. envia para <mes>.incoming/ no volume da VPS, PARALELO fluxos
    7. confere os tamanhos no servidor, renomeia para <mes>/ e ajusta o dono
    8. entrega o manifest no volume de checkpoints
    9. cria o job com `enqueue_job.py --files-ready`

O envio vai para `.incoming` e só vira `<mes>/` depois de conferido porque o
download_step trata qualquer arquivo com tamanho maior que zero como pronto: um
arquivo truncado no lugar final seria aceito pelo worker e só barraria horas
depois, no verify_step.

Uso:
    python mac/coletor.py                    # execução normal
    python mac/coletor.py --ensaio           # para antes de renomear (passo 7)
    python mac/coletor.py --run-key 2026-10  # força o mês
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# O Python põe a pasta do script no sys.path, não a raiz do repo — sem isto o
# `from config import settings` não acha nada. Mesma armadilha que derruba
# script rodado de /tmp dentro do container.
REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import enqueue_job as ej                      # noqa: E402
from config import settings                   # noqa: E402
from logger import get_logger                 # noqa: E402
from steps import download_step               # noqa: E402
from steps.base import StepStatus             # noqa: E402

log = get_logger("coletor")

# ── Configuração do coletor ────────────────────────────────────────────────────
# Fora do .env de propósito: o config.py valida as chaves que declara, e host da
# VPS não é uma delas. Tudo aqui aceita override por variável de ambiente.
SSH_ALIAS = os.environ.get("CNPJ_SSH_ALIAS", "cnpj-vps")
VPS_DATA = os.environ.get("CNPJ_VPS_DATA", "/var/lib/docker/volumes/cnpj_etl_data/_data")
VPS_CHECKPOINTS = os.environ.get(
    "CNPJ_VPS_CHECKPOINTS", "/var/lib/docker/volumes/cnpj_etl_checkpoints/_data"
)
CONTAINER = os.environ.get("CNPJ_CONTAINER", "cnpj_worker")
RSYNC = os.environ.get("CNPJ_RSYNC", "/opt/homebrew/bin/rsync")
PARALELO = int(os.environ.get("CNPJ_PARALELO", "4"))
TENTATIVAS_ENVIO = int(os.environ.get("CNPJ_TENTATIVAS_ENVIO", "3"))
IDADE_MINIMA_H = float(os.environ.get("CNPJ_IDADE_MINIMA_H", "2"))
LOCK_PATH = Path(os.environ.get("CNPJ_LOCK", str(Path.home() / "cnpj" / "coletor.lock")))

# O worker roda como appuser, uid e gid 1001. Arquivo entregue como root fica
# ilegível para ele.
DONO_VPS = "1001:1001"

# Status de job que significam "não há nada para eu fazer".
SEM_TRABALHO = {"PENDING", "RUNNING", "FAILED"}


class ErroColetor(RuntimeError):
    """Falha que interrompe a execução."""


class NadaAFazer(Exception):
    """Encerramento normal, sem trabalho pendente."""


# ── SSH ───────────────────────────────────────────────────────────────────────

def _ssh(remoto: str, *, entrada: str | None = None, timeout: int = 300) -> str:
    """Roda um comando na VPS e devolve o stdout. Levanta ErroColetor se falhar."""
    cp = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", SSH_ALIAS, remoto],
        input=entrada, capture_output=True, text=True, timeout=timeout,
    )
    if cp.returncode != 0:
        raise ErroColetor(
            f"ssh falhou (rc={cp.returncode}): {cp.stderr.strip() or cp.stdout.strip()}"
        )
    return cp.stdout


def _cmd_container(*argv: str) -> str:
    """
    Comando `docker exec` para rodar algo dentro do cnpj_worker.

    O `-w /app` e o PYTHONPATH não são decorativos: sem eles o Python põe a
    pasta do script no path e o import de `config` falha.
    """
    base = ["docker", "exec", "-w", "/app", "-e", "PYTHONPATH=/app", CONTAINER]
    return " ".join(shlex.quote(a) for a in [*base, *argv])


def _json_da_saida(texto: str):
    """
    Extrai o JSON do stdout de um comando.

    O logger do projeto escreve em stdout junto com o resultado, então a saída
    pode vir com linhas de log antes do JSON.
    """
    linhas = texto.splitlines()
    for i, linha in enumerate(linhas):
        if linha.strip() == "null" or linha.startswith(("{", "[")):
            try:
                return json.loads("\n".join(linhas[i:]))
            except json.JSONDecodeError:
                continue
    raise ErroColetor(f"saida sem JSON reconhecivel: {texto[:400]!r}")


# ── Passos ────────────────────────────────────────────────────────────────────

def passo(n: int, titulo: str) -> None:
    agora = datetime.now().strftime("%H:%M:%S")
    log.info(f"[{n}/9] {agora} — {titulo}")


def idade_da_publicacao(arquivos: list[dict]) -> float | None:
    """
    Horas desde a modificação do arquivo mais recente do mês, ou None se a
    listagem não trouxer data utilizável.
    """
    datas = []
    for f in arquivos:
        bruto = f.get("modified")
        if not bruto:
            continue
        try:
            datas.append(parsedate_to_datetime(bruto))
        except (TypeError, ValueError):
            continue
    if not datas:
        return None
    mais_recente = max(datas)
    if mais_recente.tzinfo is None:
        mais_recente = mais_recente.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - mais_recente).total_seconds() / 3600


def decidir_pelo_status(run_key: str, ensaio: bool = False) -> None:
    """
    Consulta o job na VPS e levanta NadaAFazer quando não há trabalho.

    A VPS é a fonte da verdade — de propósito. Estado guardado aqui poderia
    divergir do que o worker realmente fez.

    Em ensaio a decisão é só registrada, nunca aplicada: o ensaio é feito de
    propósito com um mês que já está SUCCESS, porque é o único jeito de exercitar
    o caminho inteiro sem risco de criar job ou mexer em dado de produção. Sair
    aqui deixaria o ensaio sem nada para fazer.
    """
    saida = _ssh(_cmd_container("python", "enqueue_job.py", "--status",
                                "--run-key", run_key), timeout=120)
    job = _json_da_saida(saida)

    if job is None:
        log.info(f"{run_key}: nenhum job na VPS — ha trabalho")
        return

    status = job.get("status")
    log.info(f"{run_key}: job {job.get('job_id')} status={status} "
             f"tentativas={job.get('attempts')}/{job.get('max_attempts')}")

    if ensaio:
        log.warning(f"ensaio: seguindo apesar de status={status}. "
                    "Nada sera renomeado, apagado ou enfileirado.")
        return

    if status == "SUCCESS":
        apagar_zips_locais(run_key)
        raise NadaAFazer(f"{run_key} ja esta SUCCESS na VPS")

    if status == "DEAD":
        log.error(
            f"ATENCAO: job de {run_key} esta DEAD na VPS — tentativas esgotadas. "
            f"Precisa de intervencao manual. Ultimo erro: {job.get('last_error')}"
        )
        raise NadaAFazer(f"{run_key} esta DEAD — nao vou mexer")

    if status in SEM_TRABALHO:
        # FAILED inclusive: o worker retenta sozinho até max_attempts.
        raise NadaAFazer(f"{run_key} esta {status} na VPS — o worker cuida")

    raise ErroColetor(f"{run_key}: status inesperado {status!r} no job_queue")


def apagar_zips_locais(run_key: str) -> None:
    """Apaga os ZIPs do mês. Só é chamado depois do job dar SUCCESS."""
    d = settings.data_dir / run_key
    if not d.is_dir():
        return
    apagados = liberados = 0
    for p in sorted(d.iterdir()):
        if p.is_file() and (p.suffix == ".zip" or p.name.endswith(".zip.part")):
            liberados += p.stat().st_size
            p.unlink()
            apagados += 1
    if apagados:
        log.info(f"{run_key}: {apagados} arquivo(s) apagados, "
                 f"{liberados / 1e9:.2f} GB liberados")


def baixar(run_key: str, arquivos: list[dict]) -> None:
    """Salva o manifest e roda o download_step de produção."""
    ej._save_manifest(run_key, arquivos)
    log.info(f"manifest salvo: {ej._manifest_path(run_key)}")

    ck = settings.checkpoint_dir / "coletor" / run_key
    ck.mkdir(parents=True, exist_ok=True)

    inicio = time.monotonic()
    res = download_step.run(job_id="coletor", run_key=run_key, checkpoint_dir=ck)
    if res.status is not StepStatus.SUCCESS:
        raise ErroColetor(f"download_step falhou: {res.error}")
    log.info(f"download concluido em {(time.monotonic() - inicio) / 60:.1f} min "
             f"({res.metadata.get('count')} arquivos)")


def conferir_local(run_key: str) -> dict[str, int]:
    """
    Confere tamanho contra o manifest e abre cada ZIP. Devolve {nome: tamanho}.

    O `check_local_files` é o mesmo código que a VPS roda no `--files-ready`, de
    propósito: divergência entre as duas conferências seria pior que nenhuma.
    """
    conf = ej.check_local_files(run_key)
    if not conf["ok"]:
        raise ErroColetor(
            f"conferencia local de {run_key} falhou ({conf.get('reason')}): "
            f"{conf.get('problemas', [])[:10]}"
        )
    log.info(f"tamanhos OK: {conf['conferidos']}/{conf['esperados']} arquivos, "
             f"{conf['bytes'] / 1e9:.2f} GB")
    if conf["extras"]:
        log.warning(f"arquivos fora do manifest em {settings.data_dir / run_key}: "
                    f"{conf['extras'][:5]}")

    manifest = ej._load_manifest(run_key) or {}
    esperados = {f["name"]: int(f["size"]) for f in manifest.get("files", [])}

    d = settings.data_dir / run_key
    for nome in sorted(esperados):
        try:
            with zipfile.ZipFile(d / nome) as zf:
                if not zf.namelist():
                    raise ErroColetor(f"{nome}: ZIP sem nenhum membro")
        except zipfile.BadZipFile as exc:
            raise ErroColetor(f"{nome}: ZIP corrompido ({exc})") from exc
    log.info(f"estrutura OK: {len(esperados)} ZIPs abrem")
    return esperados


_CONFERIR_REMOTO = r"""
import json, os, sys
pasta = sys.argv[1]
esperados = json.load(sys.stdin)
problemas = []
for nome, tam in esperados.items():
    p = os.path.join(pasta, nome)
    if not os.path.exists(p):
        problemas.append("falta " + nome)
    else:
        real = os.path.getsize(p)
        if real != tam:
            problemas.append("tamanho %s: %d != %d" % (nome, real, tam))
print(json.dumps({
    "ok": not problemas,
    "existe": os.path.isdir(pasta),
    "conferidos": len(esperados) - len(problemas),
    "esperados": len(esperados),
    "problemas": problemas,
}))
"""


def conferir_no_servidor(pasta: str, esperados: dict[str, int]) -> dict:
    """Confere nome e tamanho de cada arquivo numa pasta da VPS."""
    remoto = f"python3 -c {shlex.quote(_CONFERIR_REMOTO)} {shlex.quote(pasta)}"
    saida = _ssh(remoto, entrada=json.dumps(esperados), timeout=300)
    return _json_da_saida(saida)


def _enviar_um(origem: Path, destino: str) -> None:
    """Envia um arquivo com rsync, com retentativa. `--partial` retoma."""
    ultimo = ""
    for tentativa in range(1, TENTATIVAS_ENVIO + 1):
        cp = subprocess.run(
            [RSYNC, "-a", "--partial", "--timeout=300", str(origem), destino],
            capture_output=True, text=True,
        )
        if cp.returncode == 0:
            return
        ultimo = (cp.stderr or cp.stdout).strip()
        log.warning(f"{origem.name}: rsync falhou na tentativa {tentativa}"
                    f"/{TENTATIVAS_ENVIO} (rc={cp.returncode}): {ultimo}")
        if tentativa < TENTATIVAS_ENVIO:
            time.sleep(5 * tentativa)
    raise ErroColetor(f"{origem.name}: rsync falhou {TENTATIVAS_ENVIO}x: {ultimo}")


def enviar(run_key: str, esperados: dict[str, int]) -> None:
    """Envia os ZIPs para <mes>.incoming/ na VPS, PARALELO fluxos."""
    incoming = f"{VPS_DATA}/{run_key}.incoming"
    _ssh(f"mkdir -p {shlex.quote(incoming)}", timeout=60)

    d = settings.data_dir / run_key
    destino = f"{SSH_ALIAS}:{incoming}/"
    nomes = sorted(esperados)

    inicio = time.monotonic()
    # Um arquivo por processo: uma conexão sozinha fica em ~0,85 MB/s até a
    # França por latência, não por falta de banda.
    with ThreadPoolExecutor(max_workers=PARALELO) as pool:
        list(pool.map(lambda n: _enviar_um(d / n, destino), nomes))

    minutos = (time.monotonic() - inicio) / 60
    total = sum(esperados.values())
    log.info(f"envio concluido em {minutos:.1f} min "
             f"({total / 1e9:.2f} GB, {total / 1e6 / (minutos * 60):.2f} MB/s)")


def estado_do_destino_final(run_key: str, esperados: dict[str, int]) -> str:
    """
    'ausente', 'completo' ou 'divergente' para <mes>/ na VPS.

    'completo' cobre a execução anterior que entregou tudo e morreu antes de
    criar o job: reenviar seria desperdício, e abortar deixaria o mês parado
    para sempre esperando alguém olhar.
    """
    final = f"{VPS_DATA}/{run_key}"
    conf = conferir_no_servidor(final, esperados)
    if not conf["existe"]:
        return "ausente"
    return "completo" if conf["ok"] else "divergente"


def renomear_no_servidor(run_key: str) -> None:
    """Move <mes>.incoming/ para <mes>/ e ajusta o dono para o appuser."""
    incoming = f"{VPS_DATA}/{run_key}.incoming"
    final = f"{VPS_DATA}/{run_key}"
    script = (
        f"set -e; "
        f"[ -d {shlex.quote(incoming)} ] || {{ echo 'incoming nao existe' >&2; exit 1; }}; "
        f"[ -e {shlex.quote(final)} ] && {{ echo 'destino final ja existe' >&2; exit 2; }}; "
        f"mv {shlex.quote(incoming)} {shlex.quote(final)}; "
        f"chown -R {DONO_VPS} {shlex.quote(final)}; "
        f"echo OK"
    )
    _ssh(script, timeout=300)
    log.info(f"{run_key}: renomeado para {final}, dono {DONO_VPS}")


def entregar_manifest(run_key: str) -> None:
    """Copia o manifest para o volume de checkpoints da VPS."""
    local = ej._manifest_path(run_key)
    if not local.exists():
        raise ErroColetor(f"manifest local nao existe: {local}")
    _enviar_um(local, f"{SSH_ALIAS}:{VPS_CHECKPOINTS}/")
    remoto = f"{VPS_CHECKPOINTS}/{local.name}"
    _ssh(f"chown {DONO_VPS} {shlex.quote(remoto)}", timeout=60)
    log.info(f"manifest entregue em {remoto}")


def criar_job(run_key: str) -> None:
    """Dispara o --files-ready na VPS, que confere tudo de novo antes de criar."""
    saida = _ssh(_cmd_container("python", "enqueue_job.py", "--files-ready",
                                "--run-key", run_key), timeout=300)
    log.info(f"--files-ready: {saida.strip()}")


# ── Trava ─────────────────────────────────────────────────────────────────────

@contextmanager
def trava():
    """Impede duas execuções ao mesmo tempo. O macOS não traz o comando flock."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    f = open(LOCK_PATH, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise ErroColetor(f"outra execucao em andamento (trava {LOCK_PATH})")
    try:
        f.write(f"{os.getpid()}\n")
        f.flush()
        yield
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


# ── Fluxo ─────────────────────────────────────────────────────────────────────

def coletar(run_key_forcado: str | None = None, ensaio: bool = False) -> None:
    passo(1, "detectando o mes publicado na Receita")
    run_key, arquivos = ej.detect_wanted(run_key_forcado)
    total = sum(int(a.get("size", 0)) for a in arquivos)
    log.info(f"mes={run_key} arquivos={len(arquivos)} tamanho={total / 1e9:.2f} GB")

    passo(2, "conferindo a idade da publicacao")
    idade = idade_da_publicacao(arquivos)
    if idade is None:
        log.warning("listagem sem data de modificacao — seguindo sem essa guarda")
    elif idade < IDADE_MINIMA_H:
        raise NadaAFazer(
            f"{run_key} foi publicado ha {idade:.1f}h, menos que {IDADE_MINIMA_H}h — "
            "a Receita pode estar no meio da publicacao"
        )
    else:
        log.info(f"publicado ha {idade:.1f}h")

    passo(3, "consultando o status do job na VPS")
    decidir_pelo_status(run_key, ensaio=ensaio)

    passo(4, "baixando os ZIPs")
    baixar(run_key, arquivos)

    passo(5, "conferindo os arquivos baixados")
    esperados = conferir_local(run_key)

    passo(6, "enviando para a VPS")
    incoming = f"{VPS_DATA}/{run_key}.incoming"

    if ensaio:
        # Sem olhar o destino final: no ensaio ele existe e está vazio, porque o
        # cleanup_step já passou por ali. `.incoming` é território descartável.
        log.warning(f"ensaio: enviando para {incoming} e parando na conferencia")
        enviar(run_key, esperados)

        passo(7, "conferindo no servidor (ensaio: nao renomeia)")
        conf = conferir_no_servidor(incoming, esperados)
        if not conf["ok"]:
            raise ErroColetor(
                f"conferencia no servidor falhou "
                f"({conf['conferidos']}/{conf['esperados']}): {conf['problemas'][:10]}"
            )
        log.info(f"servidor OK: {conf['conferidos']}/{conf['esperados']} arquivos")
        raise NadaAFazer(
            f"ensaio concluido: {conf['conferidos']} arquivos batem em {incoming}. "
            "Nada foi renomeado e nenhum job criado — apague a pasta na mao."
        )

    estado = estado_do_destino_final(run_key, esperados)
    if estado == "divergente":
        raise ErroColetor(
            f"{VPS_DATA}/{run_key} ja existe na VPS e nao bate com o manifest — "
            "nao vou mexer. Confira na mao antes de rodar de novo."
        )
    if estado == "completo":
        log.warning(f"{run_key} ja estava entregue em {VPS_DATA}/{run_key} e bate "
                    "com o manifest — pulando envio e renomeacao")
    else:
        enviar(run_key, esperados)

        passo(7, "conferindo no servidor e renomeando")
        conf = conferir_no_servidor(incoming, esperados)
        if not conf["ok"]:
            raise ErroColetor(
                f"conferencia no servidor falhou "
                f"({conf['conferidos']}/{conf['esperados']}): {conf['problemas'][:10]}"
            )
        log.info(f"servidor OK: {conf['conferidos']}/{conf['esperados']} arquivos")
        renomear_no_servidor(run_key)

    passo(8, "entregando o manifest")
    entregar_manifest(run_key)

    passo(9, "criando o job na VPS")
    criar_job(run_key)

    log.info(f"{run_key}: entregue. O worker da VPS leva ~4h daqui ate SUCCESS.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Baixa os dados da Receita e entrega na VPS para processamento"
    )
    parser.add_argument("--run-key", help="forca o mes AAAA-MM (default: detecta)")
    parser.add_argument("--ensaio", action="store_true",
                        help="para antes de renomear no servidor e nao cria job")
    args = parser.parse_args()

    inicio = time.monotonic()
    log.info(f"=== coletor iniciado {datetime.now().isoformat(timespec='seconds')} "
             f"{'(ENSAIO)' if args.ensaio else ''}")
    try:
        with trava():
            coletar(args.run_key, ensaio=args.ensaio)
    except NadaAFazer as motivo:
        log.info(f"encerrando: {motivo}")
        return 0
    except ErroColetor as erro:
        log.error(f"FALHA: {erro}")
        return 1
    except Exception as erro:  # rede, SSH, disco
        log.exception(f"FALHA inesperada: {erro}")
        return 1
    finally:
        log.info(f"=== coletor terminou em {(time.monotonic() - inicio) / 60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
