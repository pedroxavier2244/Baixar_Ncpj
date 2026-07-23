# Design: Agendador diário de detecção da RF (scheduler)

**Data:** 2026-07-23
**Status:** Aprovado (design) — aguardando revisão do spec escrito
**Contexto:** Hoje, toda vez que a Receita Federal publica os dados do mês corrente (em dia imprevisível), alguém precisa rodar `python enqueue_job.py` manualmente na VPS para disparar a atualização do banco da API. O objetivo é automatizar esse disparo: checar o site da RF uma vez por dia e, ao detectar arquivo novo, enfileirar o job automaticamente.

---

## Problema

O pipeline já detecta e carrega dados novos de ponta a ponta:

- `enqueue_job.py` → checa o WebDAV da RF (PROPFIND), detecta o `run_key` (YYYY-MM), compara etags contra um manifesto salvo e cria/reenfileira um job `PENDING` no SQLite quando há mudança.
- `worker.py` (container `cnpj_worker`, roda 24/7) → consome o job e executa os 7 steps (download → verify → extract → transform → load → cleanup → index).

O único elo faltante é um **gatilho agendado** para `enqueue_job.py`. Não há nenhum componente que rode "1x por dia" sozinho — hoje depende de execução manual.

---

## Solução — visão geral

Adicionar um serviço `scheduler` novo ao `docker-compose.yml`, usando a mesma imagem do worker, rodando um loop Python leve (`scheduler.py`) que:

1. Acorda periodicamente (poll a cada ~30 min).
2. Quando é a janela do dia (≥ 03h, horário de Brasília, e ainda não checou hoje), chama a lógica de detecção/enfileiramento de `enqueue_job.py`.
3. Registra o resultado **apenas no log** (sem canal de notificação externo — decisão do usuário). Status permanece visível via `/runs` e `/health`.

O fluxo completo passa a ser autônomo:

```
scheduler (≈03h BRT) → check_and_enqueue → job PENDING no SQLite
                                               ↓
cnpj_worker (já roda 24/7) → download → verify → extract → transform → load → cleanup → index
                                               ↓
                            /runs e /health refletem o resultado
```

**Nada precisa rodar na máquina do usuário.** Tudo vive nos containers Docker da VPS (`restart: unless-stopped`), que sobem sozinhos inclusive após reboot do servidor.

---

## Componente novo: `scheduler.py`

Loop irmão do `worker.py`, mesmo estilo (nunca morre; erros inesperados são logados e o loop continua). A decisão de "checar agora?" é isolada numa **função pura**, testável sem `sleep` nem relógio real:

```python
def should_check(now: datetime, hour: int, last_check: date | None) -> bool:
    """True quando já passou da hora-alvo e ainda não checamos hoje."""
    return now.hour >= hour and last_check != now.date()
```

Esboço do loop:

```python
def main() -> None:
    init_db()
    if not settings.scheduler_enabled:
        log.warning("scheduler desabilitado (SCHEDULER_ENABLED=false) — ocioso")
        while True:
            time.sleep(3600)

    tz = ZoneInfo(settings.scheduler_tz)
    log.info(f"scheduler iniciado — checagem diária às {settings.scheduler_hour:02d}h "
             f"({settings.scheduler_tz}), poll a cada {settings.scheduler_poll_seconds}s")
    last_check: date | None = None

    while True:
        try:
            now = datetime.now(tz)
            if should_check(now, settings.scheduler_hour, last_check):
                log.info(f"janela atingida ({now:%H:%M %Z}) — checando RF")
                result = check_and_enqueue()
                if result["status"] == "error":
                    # NÃO marca o dia como checado → retenta no próximo poll (auto-cura)
                    log.error(f"checagem falhou: {result.get('reason')} — retenta no próximo poll")
                else:
                    last_check = now.date()
                    if result["status"] in ("enqueued", "requeued"):
                        log.info(f"job {result['status']}: run_key={result['run_key']} "
                                 f"job_id={result['job_id']} files={result.get('files')}")
                    else:
                        log.info(f"nada novo (status={result['status']}, run_key={result.get('run_key')})")
            else:
                # Heartbeat — confirma que o scheduler está vivo (mitigação de visibilidade)
                log.info(f"scheduler vivo — {now:%H:%M %Z}, última checagem={last_check}, "
                         f"próxima janela {settings.scheduler_hour:02d}h")
        except Exception as exc:
            log.error(f"erro inesperado no loop do scheduler: {exc}", exc_info=True)
        time.sleep(settings.scheduler_poll_seconds)
```

**Propriedades desse padrão:**

- **Auto-curável:** se a RF estiver fora do ar às 03h, `check_and_enqueue` retorna `error`, o dia não é marcado como checado, e o próximo poll (03h30) tenta de novo.
- **Resistente a restart:** ao reiniciar, `last_check` volta a `None`; se já passou das 03h, faz uma checagem extra — inofensiva, porque o enqueue é idempotente.
- **Heartbeat:** a cada poll fora da janela, loga uma linha "scheduler vivo" (≈48 linhas/dia). Permite confirmar que o container está de pé sem precisar de notificação push.

---

## Refactor: `enqueue_job.py` → função reutilizável

Hoje `enqueue_job.main()` mistura CLI (`argparse`, `print`, `sys.exit`) com a lógica de negócio. Importar e chamar `main()` do scheduler seria perigoso: os `sys.exit()` levantariam `SystemExit` e derrubariam o loop.

Extrair o núcleo em duas funções, **preservando 100% do comportamento atual do CLI**:

### `detect_wanted(run_key: str | None = None) -> tuple[str, list[dict]]`

Encapsula o PROPFIND + detecção de `run_key` + filtro de ZIPs + fallback por mês (linhas atuais 180–201). Levanta exceção em falha (sem token, listagem falhou, nenhum ZIP encontrado). Reutilizada tanto por `check_and_enqueue` quanto pelo `--check-only`.

### `check_and_enqueue(run_key: str | None = None, force: bool = False) -> dict`

Contém a lógica de decisão atual (linhas 173–228) **sem** `argparse`/`print`/`sys.exit`. Chama `detect_wanted` (dentro de try/except → converte falha operacional em `status="error"`), depois decide. Retorna um dict estruturado:

| `status` | Quando | Campos extras |
|---|---|---|
| `enqueued` | job novo criado | `run_key`, `job_id`, `files` |
| `requeued` | job existente resetado para PENDING | `run_key`, `job_id`, `files` |
| `already_success` | mês já concluído e sem `force` | `run_key` |
| `no_change` | etags iguais ao manifesto salvo | `run_key` |
| `error` | sem token / WebDAV falhou / nenhum ZIP | `reason` |

### `main()` — wrapper fino de CLI

Continua **idêntico** para quem roda na mão. Faz `argparse`, trata `--check-only` (usa `detect_wanted` e imprime o JSON como hoje), chama `check_and_enqueue(run_key, force)`, traduz o `status` retornado para a linha de `stdout` atual (`OK job_id=... run_key=...`) e o código de saída correspondente (`0` para sucesso/no_change/already_success, `1` para error). As mensagens de log existentes são preservadas.

---

## Config novo (`config.py`)

Adicionar ao `Settings`:

```python
# Scheduler — checagem automática diária da RF
scheduler_enabled: bool = True
scheduler_hour: int = 3                      # hora-alvo (0–23), horário de scheduler_tz
scheduler_tz: str = "America/Sao_Paulo"
scheduler_poll_seconds: int = 1800           # 30 min entre polls
```

Documentar as quatro variáveis no `.env.example`.

---

## Timezone — robustez (mitigação 1)

**Risco:** `python:3.12-slim` não inclui `tzdata`. Só setar `TZ=America/Sao_Paulo` no container **não** funciona — `datetime.now()` cairia para UTC silenciosamente (03h UTC = meia-noite BRT), rodando na hora errada.

**Solução — não depender do TZ do sistema:**

1. Adicionar o pacote **`tzdata`** (PyPI, base de fusos pura-Python) ao `requirements.txt`. O `zoneinfo` da stdlib usa esse pacote como fallback quando o SO não tem a base de fusos.
2. No `scheduler.py`, usar `ZoneInfo(settings.scheduler_tz)` explicitamente. O horário fica correto independente do ambiente do container.
3. Manter `TZ=America/Sao_Paulo` no serviço do compose como reforço (afeta exibição de datas), mas o `zoneinfo` é a fonte de verdade da decisão.

*(O Brasil não tem horário de verão desde 2019, então não há complicação de DST hoje; usar `zoneinfo` mantém a correção mesmo se isso mudar.)*

**Validação obrigatória no deploy:**

```bash
docker exec cnpj_scheduler python -c \
  "from datetime import datetime; from zoneinfo import ZoneInfo; print(datetime.now(ZoneInfo('America/Sao_Paulo')))"
# Deve imprimir a hora local de Brasília, não UTC
```

---

## Serviço no `docker-compose.yml`

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

**Pontos críticos:**

- **`etl_checkpoints:/checkpoints` é obrigatório e compartilhado com o worker.** O scheduler grava o job no `pipeline_control.db` (SQLite) e o manifesto WebDAV no volume de checkpoints; o worker lê o job do mesmo `pipeline_control.db`. Sem esse volume compartilhado, o worker nunca veria o job.
- **Sem `implementation_default`.** O scheduler só fala com o WebDAV (internet, via `cnpj_net`) e o SQLite. Não toca no PostgreSQL — não precisa da rede do banco.
- **Sem `depends_on`.** Não depende de Redis/API/Postgres. `init_db()` é idempotente.
- **Sem `REDIS_URL`.** O scheduler não usa cache.

---

## Alteração no `Dockerfile`

`scheduler.py` precisa ser copiado para a imagem. Adicionar à linha de `COPY` existente:

```dockerfile
COPY config.py   logger.py orchestrator.py worker.py \
     enqueue_job.py scheduler.py setup.py ./
```

*(O `tzdata` entra via `requirements.txt`, então o `pip install` do build stage já o inclui — nenhum `apt-get` novo é necessário.)*

---

## Tratamento de erros / resiliência

| Situação | Comportamento |
|---|---|
| WebDAV fora do ar / token inválido | `check_and_enqueue` retorna `error`; o dia **não** é marcado; retenta no próximo poll |
| Exceção inesperada no loop | Capturada, logada com `exc_info`, loop continua (nunca derruba o container) |
| Mês já carregado (SUCCESS) | `already_success` → nenhum job criado (idempotência) |
| Arquivos sem mudança (etags iguais) | `no_change` → nenhum job criado |
| Container reiniciado no meio do dia | `last_check=None`; checagem extra inofensiva (enqueue idempotente) |
| `scheduler_enabled=false` | Loga aviso e fica ocioso (kill-switch sem editar o compose) |

---

## Testes (`tests/test_scheduler.py`)

Seguindo os fixtures/pytest já existentes em `tests/`:

1. **`should_check` (função pura, `now` injetado):**
   - antes da hora-alvo → `False`
   - na/depois da hora-alvo e sem checagem hoje → `True`
   - depois da hora-alvo mas já checado hoje → `False`
   - virada de dia reseta (checou ontem, novo dia → `True`)

2. **`check_and_enqueue` com `propfind_listing` mockado (monkeypatch), usando `control_db`/`checkpoint_dir` temporários:**
   - listagem nova, sem manifesto → `enqueued` (job criado no SQLite temp)
   - listagem igual ao manifesto salvo → `no_change`
   - job existente já `SUCCESS`, sem `force` → `already_success`
   - `propfind_listing` levanta exceção → `error`

3. **CLI preservado:** `main()` com `--check-only` ainda imprime o JSON esperado; sem argumentos ainda imprime `OK job_id=...` ou sai com código `1` em erro.

---

## O que NÃO muda

- `worker.py`, `orchestrator.py`, os 7 steps — intocados.
- `db/control.py` — intocado (`create_job`/`requeue_job`/`get_job_by_run_key`/`init_db` reutilizados como estão).
- API, rotas (`/runs`, `/health`), nginx, Redis — intocados.
- Comportamento do CLI `python enqueue_job.py` (todas as flags) — idêntico.
- Nenhuma tabela `cnpj.*` é tocada pelo scheduler.

---

## Arquivos alterados

| Arquivo | Mudança |
|---|---|
| `scheduler.py` | **Novo** — loop + `should_check` |
| `enqueue_job.py` | Extrair `detect_wanted` + `check_and_enqueue`; `main()` vira wrapper de CLI |
| `config.py` | Adicionar 4 settings `scheduler_*` |
| `docker-compose.yml` | Adicionar serviço `scheduler` |
| `Dockerfile` | Adicionar `scheduler.py` à linha `COPY` |
| `requirements.txt` | Adicionar `tzdata` |
| `.env.example` | Documentar as 4 variáveis `scheduler_*` |
| `tests/test_scheduler.py` | **Novo** — testes de `should_check` e `check_and_enqueue` |

---

## Critérios de sucesso

1. O container `cnpj_scheduler` sobe com `docker compose up -d` e loga "scheduler iniciado".
2. `docker exec cnpj_scheduler python -c "..."` confirma horário de Brasília (não UTC).
3. Ao passar das 03h BRT, o scheduler chama `check_and_enqueue` e loga o resultado.
4. Com arquivo novo na RF, um job `PENDING` aparece e o worker o processa — visível em `/runs`.
5. Rodar todo dia é seguro: mês já carregado → `already_success`/`no_change`, sem recarga.
6. Falha transitória do WebDAV não trava nem duplica: o loop retenta no próximo poll.
7. `python enqueue_job.py` (todas as flags) continua funcionando exatamente como antes.
8. Heartbeat no log permite confirmar que o scheduler está vivo entre janelas.
