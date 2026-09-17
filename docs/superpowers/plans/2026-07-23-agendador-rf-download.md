# Agendador Diário de Detecção da RF — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adicionar um serviço `scheduler` que checa o WebDAV da Receita Federal 1x/dia (~03h BRT) e dispara `enqueue_job` automaticamente, sem intervenção manual, registrando tudo apenas em log.

**Architecture:** Um novo container `cnpj_scheduler` (mesma imagem do worker) roda um loop leve em `scheduler.py`. A cada poll (~30 min) ele verifica, via função pura `should_check`, se já passou da hora-alvo e ainda não checou hoje; em caso positivo chama `check_and_enqueue` — a lógica de detecção/enfileiramento extraída de `enqueue_job.py`. O job criado no SQLite (`pipeline_control.db`, volume compartilhado com o worker) é consumido pelo `cnpj_worker`, que já roda 24/7.

**Tech Stack:** Python 3.12, `zoneinfo` + pacote `tzdata`, SQLite (fila de jobs), Docker Compose, httpx (WebDAV PROPFIND), pytest.

## Global Constraints

- **Encoding UTF-8 obrigatório** em todos os arquivos criados/editados (o histórico tem casos de mojibake).
- **O CLI `python enqueue_job.py` deve continuar idêntico** — todas as flags (`--run-key`, `--force`, `--check-only`) e a saída de `stdout`/exit codes preservadas.
- **Só log** — nenhum canal de notificação externo (Telegram/e-mail/webhook). Status permanece visível via `/runs` e `/health`.
- **O scheduler compartilha o volume `etl_checkpoints` com o worker** — é onde vivem o `pipeline_control.db` e os manifestos WebDAV. Sem isso o worker não vê o job.
- **O scheduler NÃO acessa PostgreSQL nem Redis** — só WebDAV (internet via `cnpj_net`) e SQLite.
- **Nenhuma tabela `cnpj.*` é tocada** por este trabalho.
- **Timezone via `zoneinfo.ZoneInfo`**, nunca dependendo apenas do `TZ` do container (`python:3.12-slim` não traz tzdata do SO).

---

## File Structure

| Arquivo | Responsabilidade |
|---|---|
| `config.py` (modificar) | 4 settings novas `scheduler_*` |
| `requirements.txt` (modificar) | Dependência `tzdata` (fallback do `zoneinfo`) |
| `.env.example` (modificar) | Documentar as 4 variáveis `SCHEDULER_*` |
| `enqueue_job.py` (modificar) | Extrair `detect_wanted` + `check_and_enqueue`; `main()` vira wrapper de CLI |
| `scheduler.py` (criar) | Loop do agendador + função pura `should_check` |
| `Dockerfile` (modificar) | Copiar `scheduler.py` para a imagem |
| `docker-compose.yml` (modificar) | Serviço `scheduler` |
| `tests/test_config.py` (modificar) | Teste das defaults `scheduler_*` |
| `tests/test_scheduler.py` (criar) | Testes de `check_and_enqueue` e `should_check` |

---

## Task 1: Config, dependências e documentação de ambiente

**Files:**
- Modify: `config.py` (bloco de settings — após `index_parallel_workers`)
- Modify: `requirements.txt` (seção Core)
- Modify: `.env.example` (final do arquivo)
- Test: `tests/test_config.py` (novo teste ao final)

**Interfaces:**
- Consumes: nada.
- Produces: `settings.scheduler_enabled: bool`, `settings.scheduler_hour: int`, `settings.scheduler_tz: str`, `settings.scheduler_poll_seconds: int`.

- [ ] **Step 1: Escrever o teste que falha**

Adicionar ao final de `tests/test_config.py`:

```python
def test_scheduler_defaults():
    from config import Settings
    s = Settings()
    assert s.scheduler_enabled is True
    assert s.scheduler_hour == 3
    assert s.scheduler_tz == "America/Sao_Paulo"
    assert s.scheduler_poll_seconds == 1800
```

- [ ] **Step 2: Rodar o teste e confirmar que falha**

Run: `python -m pytest tests/test_config.py::test_scheduler_defaults -v`
Expected: FAIL com `AttributeError: 'Settings' object has no attribute 'scheduler_enabled'`

- [ ] **Step 3: Adicionar as settings ao `config.py`**

Em `config.py`, logo após a linha `index_parallel_workers: int = 0   # ...` (fim do bloco "Index step"), inserir:

```python

    # Scheduler — checagem automática diária da RF
    scheduler_enabled: bool = True
    scheduler_hour: int = 3                      # hora-alvo (0-23), no fuso scheduler_tz
    scheduler_tz: str = "America/Sao_Paulo"
    scheduler_poll_seconds: int = 1800           # 30 min entre polls
```

- [ ] **Step 4: Adicionar `tzdata` ao `requirements.txt`**

Na seção `# Core`, logo após `python-dotenv>=1.0`, inserir:

```
# Timezone — base de fusos para zoneinfo (python:slim não traz tzdata do SO)
tzdata>=2024.1
```

- [ ] **Step 5: Documentar as variáveis no `.env.example`**

Ao final de `.env.example`, adicionar:

```
# Scheduler — checagem automática diária da RF
SCHEDULER_ENABLED=true
SCHEDULER_HOUR=3
SCHEDULER_TZ=America/Sao_Paulo
SCHEDULER_POLL_SECONDS=1800
```

- [ ] **Step 6: Rodar o teste e confirmar que passa**

Run: `python -m pytest tests/test_config.py::test_scheduler_defaults -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add config.py requirements.txt .env.example tests/test_config.py
git commit -m "feat(config): settings do scheduler + dependencia tzdata"
```

---

## Task 2: Refatorar `enqueue_job.py` em `detect_wanted` + `check_and_enqueue`

Extrai a lógica de detecção/enfileiramento de `main()` para funções reutilizáveis, sem `sys.exit`, preservando 100% do comportamento do CLI. É a mudança mais delicada — feita sob testes.

**Files:**
- Modify: `enqueue_job.py` (adicionar `detect_wanted` e `check_and_enqueue`; reescrever `main`)
- Create: `tests/test_scheduler.py` (testes de `check_and_enqueue`)

**Interfaces:**
- Consumes: `propfind_listing(token, rel_path="") -> list[dict]`, `_detect_run_key(items) -> str`, `_filter_wanted(items) -> list[dict]`, `_load_manifest(run_key) -> dict | None`, `_save_manifest(run_key, items)`, `_files_changed(old, new_items) -> bool` (já existentes em `enqueue_job.py`); `create_job`, `requeue_job`, `get_job_by_run_key`, `init_db` (de `db.control`).
- Produces:
  - `detect_wanted(run_key: str | None = None) -> tuple[str, list[dict]]` — levanta `RuntimeError` se sem token ou sem ZIPs; exceções de rede propagam.
  - `check_and_enqueue(run_key: str | None = None, force: bool = False) -> dict` — nunca chama `sys.exit`. Retorna dict com chave `"status"` ∈ `{"enqueued","requeued","already_success","no_change","error"}`. Campos: `run_key` (exceto quando `error`), `job_id` e `files` (só em `enqueued`/`requeued`), `reason` (só em `error`).

- [ ] **Step 1: Escrever os testes que falham**

Criar `tests/test_scheduler.py`:

```python
from unittest.mock import patch


def _fake_items(etag="aaa"):
    """Uma listagem WebDAV mínima com um ZIP cujo nome contém o mês."""
    return [{
        "name": "Empresas_2026-07.zip",
        "path": "Empresas_2026-07.zip",
        "is_dir": False,
        "size": 10,
        "modified": "",
        "etag": etag,
    }]


def _settings_patch(tmp_path):
    """Redireciona control_db, dirs e token para um ambiente isolado."""
    return patch.multiple(
        "config.settings",
        control_db=tmp_path / "control.db",
        checkpoint_dir=tmp_path / "checkpoints",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        webdav_token="fake-token",
    )


def test_check_and_enqueue_creates_job(tmp_path):
    with _settings_patch(tmp_path), \
         patch("enqueue_job.propfind_listing", return_value=_fake_items("aaa")):
        from enqueue_job import check_and_enqueue
        result = check_and_enqueue()
        assert result["status"] == "enqueued"
        assert result["run_key"] == "2026-07"
        assert result["files"] == 1
        assert result["job_id"]


def test_check_and_enqueue_no_change_second_call(tmp_path):
    with _settings_patch(tmp_path), \
         patch("enqueue_job.propfind_listing", return_value=_fake_items("aaa")):
        from enqueue_job import check_and_enqueue
        first = check_and_enqueue()
        assert first["status"] == "enqueued"
        second = check_and_enqueue()
        assert second["status"] == "no_change"
        assert second["run_key"] == "2026-07"


def test_check_and_enqueue_already_success(tmp_path):
    with _settings_patch(tmp_path), \
         patch("enqueue_job.propfind_listing", return_value=_fake_items("aaa")):
        from enqueue_job import check_and_enqueue
        from db.control import finish_job
        first = check_and_enqueue()
        finish_job(first["job_id"], success=True)
        again = check_and_enqueue()
        assert again["status"] == "already_success"
        assert again["run_key"] == "2026-07"


def test_check_and_enqueue_webdav_error(tmp_path):
    def boom(*args, **kwargs):
        raise RuntimeError("connection refused")
    with _settings_patch(tmp_path), \
         patch("enqueue_job.propfind_listing", side_effect=boom):
        from enqueue_job import check_and_enqueue
        result = check_and_enqueue()
        assert result["status"] == "error"
        assert "connection refused" in result["reason"]
```

- [ ] **Step 2: Rodar os testes e confirmar que falham**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: FAIL com `ImportError: cannot import name 'check_and_enqueue' from 'enqueue_job'`

- [ ] **Step 3: Adicionar `detect_wanted` e `check_and_enqueue` ao `enqueue_job.py`**

Em `enqueue_job.py`, inserir estas duas funções logo **antes** de `def main() -> None:`:

```python
def detect_wanted(run_key: str | None = None) -> tuple[str, list[dict]]:
    """
    Consulta o WebDAV da RF e devolve (run_key, lista de ZIPs desejados).
    Levanta RuntimeError se não houver token ou se nenhum ZIP for encontrado.
    Falhas de rede (PROPFIND) propagam como exceção httpx.
    """
    if not settings.webdav_token:
        raise RuntimeError("WEBDAV_TOKEN is not set in .env")

    items = propfind_listing(settings.webdav_token)
    run_key = run_key or _detect_run_key(items)
    wanted = _filter_wanted(items)
    if not wanted:
        month_items = propfind_listing(settings.webdav_token, rel_path=f"{run_key}/")
        wanted = _filter_wanted(month_items)
    if not wanted:
        raise RuntimeError(
            "no wanted ZIPs found in WebDAV listing - check token/URL and wanted_files config"
        )
    return run_key, wanted


def check_and_enqueue(run_key: str | None = None, force: bool = False) -> dict:
    """
    Checa o WebDAV e enfileira um job se houver dado novo/alterado.
    Nunca chama sys.exit. Retorna um dict com a chave "status":
      enqueued | requeued | already_success | no_change | error
    """
    init_db()
    settings.ensure_dirs()

    try:
        run_key, wanted = detect_wanted(run_key)
    except Exception as exc:
        log.error(f"WebDAV detection failed: {exc}")
        return {"status": "error", "reason": str(exc)}

    log.info(f"detected run_key={run_key}, files={len(wanted)}")

    existing = get_job_by_run_key(run_key)
    if existing and existing["status"] == "SUCCESS" and not force:
        log.info(f"run_key={run_key} already SUCCESS - nothing to do")
        return {"status": "already_success", "run_key": run_key}

    old_manifest = _load_manifest(run_key)
    if not _files_changed(old_manifest, wanted) and not force:
        log.info(f"files unchanged since last run of {run_key} - skipping enqueue")
        return {"status": "no_change", "run_key": run_key}

    _save_manifest(run_key, wanted)
    payload = {"files": wanted, "enqueued_at": datetime.now(timezone.utc).isoformat()}
    if existing:
        job_id = requeue_job(run_key, payload=payload)
        log.info(f"job reset to PENDING job_id={job_id} run_key={run_key}")
        status = "requeued"
    else:
        job_id = create_job(run_key, payload=payload)
        log.info(f"job created job_id={job_id} run_key={run_key}")
        status = "enqueued"
    return {"status": status, "run_key": run_key, "job_id": job_id, "files": len(wanted)}
```

- [ ] **Step 4: Reescrever `main()` como wrapper fino de CLI**

Substituir todo o corpo atual de `def main() -> None:` (do `parser = argparse...` até o fim da função) por:

```python
def main() -> None:
    parser = argparse.ArgumentParser(description="Enqueue CNPJ ETL job")
    parser.add_argument("--run-key", help="YYYY-MM to process (default: auto-detect)")
    parser.add_argument("--force", action="store_true",
                        help="Re-enqueue even if already SUCCESS")
    parser.add_argument("--check-only", action="store_true",
                        help="Only check status, do not create job")
    args = parser.parse_args()

    init_db()
    settings.ensure_dirs()

    log.info("checking Receita Federal WebDAV for new files...")

    if args.check_only:
        try:
            run_key, wanted = detect_wanted(args.run_key)
        except Exception as exc:
            log.error(f"WebDAV detection failed: {exc}")
            sys.exit(1)
        print(json.dumps({"run_key": run_key, "files": wanted}, indent=2, ensure_ascii=False))
        sys.exit(0)

    result = check_and_enqueue(args.run_key, force=args.force)
    if result["status"] == "error":
        sys.exit(1)
    if result["status"] in ("enqueued", "requeued"):
        print(f"OK job_id={result['job_id']} run_key={result['run_key']}")
    sys.exit(0)
```

- [ ] **Step 5: Rodar os testes e confirmar que passam**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: PASS (4 testes)

- [ ] **Step 6: Rodar a suíte inteira para garantir que nada quebrou**

Run: `python -m pytest tests/test_scheduler.py tests/test_config.py tests/test_control_db.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add enqueue_job.py tests/test_scheduler.py
git commit -m "refactor(enqueue): extrai detect_wanted + check_and_enqueue reutilizaveis"
```

---

## Task 3: Criar o `scheduler.py`

O loop do agendador, com a decisão isolada na função pura `should_check`.

**Files:**
- Create: `scheduler.py`
- Test: `tests/test_scheduler.py` (adicionar testes de `should_check`)

**Interfaces:**
- Consumes: `check_and_enqueue(run_key=None, force=False) -> dict` (Task 2); `settings.scheduler_enabled/scheduler_hour/scheduler_tz/scheduler_poll_seconds` (Task 1); `init_db` (de `db.control`); `get_logger` (de `logger`).
- Produces: `should_check(now: datetime, hour: int, last_check: date | None) -> bool`; `main() -> None` (loop).

- [ ] **Step 1: Escrever os testes que falham**

Adicionar ao final de `tests/test_scheduler.py`:

```python
from datetime import date, datetime


def test_should_check_before_hour():
    from scheduler import should_check
    now = datetime(2026, 7, 23, 2, 0)   # 02h — antes da hora-alvo (03h)
    assert should_check(now, 3, None) is False


def test_should_check_at_hour_not_checked_today():
    from scheduler import should_check
    now = datetime(2026, 7, 23, 3, 30)  # 03h30, ainda não checou hoje
    assert should_check(now, 3, None) is True


def test_should_check_already_checked_today():
    from scheduler import should_check
    now = datetime(2026, 7, 23, 4, 0)   # já passou, mas já checou hoje
    assert should_check(now, 3, date(2026, 7, 23)) is False


def test_should_check_new_day_resets():
    from scheduler import should_check
    now = datetime(2026, 7, 24, 3, 5)   # novo dia, última checagem foi ontem
    assert should_check(now, 3, date(2026, 7, 23)) is True
```

- [ ] **Step 2: Rodar os testes e confirmar que falham**

Run: `python -m pytest tests/test_scheduler.py -k should_check -v`
Expected: FAIL com `ModuleNotFoundError: No module named 'scheduler'`

- [ ] **Step 3: Criar `scheduler.py`**

```python
"""
Scheduler — dispara a checagem diária da RF chamando check_and_enqueue.

Roda como container próprio (cnpj_scheduler). A cada poll verifica, via
should_check, se já é a janela do dia (>= scheduler_hour, no fuso scheduler_tz)
e ainda não checou hoje; em caso positivo chama check_and_enqueue. Registra
tudo apenas em log (sem notificação externa).

Usage:
    python scheduler.py
"""
from __future__ import annotations

import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from config import settings
from db.control import init_db
from enqueue_job import check_and_enqueue
from logger import get_logger

log = get_logger("scheduler")


def should_check(now: datetime, hour: int, last_check: date | None) -> bool:
    """True quando já passou da hora-alvo e ainda não checamos hoje."""
    return now.hour >= hour and last_check != now.date()


def main() -> None:
    init_db()

    if not settings.scheduler_enabled:
        log.warning("scheduler desabilitado (SCHEDULER_ENABLED=false) - ocioso")
        while True:
            time.sleep(3600)

    tz = ZoneInfo(settings.scheduler_tz)
    log.info(
        f"scheduler iniciado - checagem diária às {settings.scheduler_hour:02d}h "
        f"({settings.scheduler_tz}), poll a cada {settings.scheduler_poll_seconds}s"
    )
    last_check: date | None = None

    while True:
        try:
            now = datetime.now(tz)
            if should_check(now, settings.scheduler_hour, last_check):
                log.info(f"janela atingida ({now:%H:%M %Z}) - checando RF")
                result = check_and_enqueue()
                if result["status"] == "error":
                    log.error(
                        f"checagem falhou: {result.get('reason')} - "
                        f"retenta no próximo poll"
                    )
                else:
                    last_check = now.date()
                    if result["status"] in ("enqueued", "requeued"):
                        log.info(
                            f"job {result['status']}: run_key={result['run_key']} "
                            f"job_id={result['job_id']} files={result.get('files')}"
                        )
                    else:
                        log.info(
                            f"nada novo (status={result['status']}, "
                            f"run_key={result.get('run_key')})"
                        )
            else:
                # Heartbeat — confirma que o scheduler está vivo entre janelas
                log.info(
                    f"scheduler vivo - {now:%H:%M %Z}, última checagem={last_check}, "
                    f"próxima janela {settings.scheduler_hour:02d}h"
                )
        except Exception as exc:
            log.error(f"erro inesperado no loop do scheduler: {exc}", exc_info=True)
        time.sleep(settings.scheduler_poll_seconds)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Rodar os testes e confirmar que passam**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: PASS (8 testes no total — 4 de `check_and_enqueue` + 4 de `should_check`)

- [ ] **Step 5: Smoke test do módulo (import + kill-switch)**

Confirma que o módulo importa e que o modo desabilitado não explode. Rodar:

```bash
python -c "import scheduler; print(scheduler.should_check(__import__('datetime').datetime(2026,7,23,3,0), 3, None))"
```

Expected: imprime `True`

- [ ] **Step 6: Commit**

```bash
git add scheduler.py tests/test_scheduler.py
git commit -m "feat(scheduler): loop diario que dispara check_and_enqueue (log-only)"
```

---

## Task 4: Empacotar na imagem e no Compose

Coloca o `scheduler.py` dentro da imagem e sobe o serviço `cnpj_scheduler`. Sem teste unitário — validado por build/deploy.

**Files:**
- Modify: `Dockerfile` (linha `COPY` do código)
- Modify: `docker-compose.yml` (novo serviço `scheduler`)

**Interfaces:**
- Consumes: `scheduler.py` (Task 3); volume `etl_checkpoints` e rede `cnpj_net` (já existentes).
- Produces: container `cnpj_scheduler`.

- [ ] **Step 1: Adicionar `scheduler.py` ao `COPY` do `Dockerfile`**

Substituir o bloco atual (linhas 28-29):

```dockerfile
COPY config.py   logger.py orchestrator.py worker.py \
     enqueue_job.py setup.py ./
```

por:

```dockerfile
COPY config.py   logger.py orchestrator.py worker.py \
     enqueue_job.py scheduler.py setup.py ./
```

- [ ] **Step 2: Adicionar o serviço `scheduler` ao `docker-compose.yml`**

Inserir o bloco abaixo logo após o fim do serviço `worker` (depois da linha `- implementation_default` do worker, antes do comentário `# ── Redis Cache ──`):

```yaml

  # ── Scheduler — dispara a checagem diária da RF ──────────────────────────────
  scheduler:
    build: .
    container_name: cnpj_scheduler
    restart: unless-stopped
    env_file: .env
    environment:
      DATA_DIR: "/data"
      LOG_DIR: "/logs"
      CHECKPOINT_DIR: "/checkpoints"
      CONTROL_DB: "/checkpoints/pipeline_control.db"
      STATUS_FILE: "/data/ultimo_status.json"
      TZ: "America/Sao_Paulo"
    command: ["python", "scheduler.py"]
    volumes:
      - etl_data:/data
      - etl_logs:/logs
      - etl_checkpoints:/checkpoints
    networks:
      - cnpj_net
```

- [ ] **Step 3: Validar a sintaxe do Compose**

Numa máquina com Docker (a VPS, ou local se disponível):

Run: `docker compose config`
Expected: imprime a config resolvida sem erro, incluindo o serviço `scheduler` com `container_name: cnpj_scheduler` e o volume `etl_checkpoints:/checkpoints`.

*(Se não houver Docker na máquina local, pular para a validação no deploy — Step 5.)*

- [ ] **Step 4: Commit**

```bash
git add Dockerfile docker-compose.yml
git commit -m "build(scheduler): copia scheduler.py na imagem + servico no compose"
```

- [ ] **Step 5: Deploy e validação na VPS**

Estes passos rodam na VPS (bash), após `git push` local e `git pull` no servidor:

```bash
# Subir só o novo serviço (rebuild da imagem)
docker compose up -d --build scheduler

# 1. Container de pé?
docker ps --format "table {{.Names}}\t{{.Status}}" | grep cnpj_scheduler

# 2. TIMEZONE — validação crítica: deve imprimir hora de Brasília, não UTC
docker exec cnpj_scheduler python -c \
  "from datetime import datetime; from zoneinfo import ZoneInfo; print(datetime.now(ZoneInfo('America/Sao_Paulo')))"

# 3. Log de inicialização (deve conter "scheduler iniciado" e o heartbeat "scheduler vivo")
docker logs cnpj_scheduler --tail 20
```

Expected:
1. `cnpj_scheduler` aparece com `Up`.
2. A data/hora impressa bate com o horário de Brasília no momento (offset `-03:00`).
3. Os logs mostram `scheduler iniciado - checagem diária às 03h (America/Sao_Paulo)...` e, entre janelas, linhas `scheduler vivo - HH:MM ...`.

---

## Self-Review (feita pelo autor do plano)

**Cobertura do spec:**

| Requisito do spec | Task |
|---|---|
| `scheduler.py` com `should_check` + loop | Task 3 |
| Refactor `enqueue_job.py` → `detect_wanted` + `check_and_enqueue`, CLI idêntico | Task 2 |
| Settings `scheduler_*` no `config.py` | Task 1 |
| `tzdata` no `requirements.txt` + `zoneinfo` no scheduler | Task 1 (dep) + Task 3 (uso) |
| Documentar vars no `.env.example` | Task 1 |
| Serviço `scheduler` no `docker-compose.yml` (volume `etl_checkpoints`, sem rede do Postgres, sem Redis) | Task 4 |
| `scheduler.py` na linha `COPY` do `Dockerfile` | Task 4 |
| Heartbeat no log | Task 3 |
| Validação de TZ no deploy | Task 4, Step 5 |
| Testes de `should_check` e `check_and_enqueue` | Task 2 + Task 3 |
| Auto-cura (erro não marca o dia; retenta) | Task 3 (lógica) + `test_check_and_enqueue_webdav_error` (Task 2) |
| Kill-switch `scheduler_enabled` | Task 3 |

Sem lacunas.

**Placeholders:** nenhum — todo passo tem código/comando real e saída esperada.

**Consistência de tipos:** `check_and_enqueue` retorna sempre um dict com `"status"`; os consumidores (`main`, `scheduler.main`) checam `result["status"]` e acessam `result["job_id"]`/`result["run_key"]`/`result.get("reason")` exatamente como definido no bloco Produces da Task 2. `should_check(now, hour, last_check)` tem a mesma assinatura na definição (Task 3) e nos testes/chamada do loop.

---

## Notas de execução

- **Ambiente de teste:** rodar `pip install -r requirements.txt` antes dos testes (para ter `tzdata` disponível ao `zoneinfo` também no Windows/local).
- **O loop `main()` do scheduler não é testado unitariamente** (é `while True`): a lógica de risco está isolada em `should_check` e `check_and_enqueue`, ambas cobertas. O loop é validado na prática no deploy (Task 4, Step 5).
- **Ordem das tasks:** 1 → 2 → 3 → 4 (a Task 3 depende de 1 e 2; a Task 4 depende de 3).
