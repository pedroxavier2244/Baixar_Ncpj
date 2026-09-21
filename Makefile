# =============================================================================
# Makefile — atalhos para gerenciar o projeto CNPJ na VPS
# Uso: make <alvo>
# =============================================================================

COMPOSE = docker compose
WORKER  = $(COMPOSE) exec -T worker

.PHONY: up down restart build logs logs-api logs-worker \
        shell-api shell-worker shell-redis \
        enqueue status ps clean

# ── Ciclo de vida ─────────────────────────────────────────────────────────────

up:         ## Sobe todos os containers em background
	$(COMPOSE) up -d

down:       ## Para e remove todos os containers
	$(COMPOSE) down

restart:    ## Reinicia todos os containers
	$(COMPOSE) restart

build:      ## Rebuilda as imagens (após mudanças de código)
	$(COMPOSE) up -d --build

# ── Logs ──────────────────────────────────────────────────────────────────────

logs:       ## Logs de todos os containers (follow)
	$(COMPOSE) logs -f --tail=100

logs-api:   ## Logs somente da API
	$(COMPOSE) logs -f --tail=100 api

logs-worker: ## Logs somente do worker ETL
	$(COMPOSE) logs -f --tail=100 worker

# ── Shells interativos ────────────────────────────────────────────────────────

shell-api:  ## Shell bash dentro do container da API
	$(COMPOSE) exec api bash

shell-worker: ## Shell bash dentro do container do worker
	$(COMPOSE) exec worker bash

shell-redis: ## Redis CLI
	$(COMPOSE) exec redis redis-cli

# ── ETL ───────────────────────────────────────────────────────────────────────

enqueue:    ## Enfileira uma carga completa (usa os arquivos mais recentes da Receita)
	$(WORKER) python -c "from worker import enqueue_full_run; import asyncio; asyncio.run(enqueue_full_run())"

status:     ## Mostra o último status da ETL
	$(WORKER) python -c "import json,pathlib; p=pathlib.Path('/data/ultimo_status.json'); print(json.dumps(json.loads(p.read_text()), indent=2) if p.exists() else 'sem status')"

run-once:   ## Executa uma carga completa diretamente (sem fila) — use em dev
	$(WORKER) python worker.py --run-once

# ── Infraestrutura ────────────────────────────────────────────────────────────

ps:         ## Status dos containers
	$(COMPOSE) ps

clean:      ## Remove containers, volumes e imagens locais (CUIDADO: apaga dados!)
	@echo "ATENÇÃO: isso apaga volumes (dados ETL, Redis). Continuar? [y/N]" && read ans && [ "$$ans" = "y" ]
	$(COMPOSE) down -v --rmi local

# ── Ajuda ─────────────────────────────────────────────────────────────────────

help:       ## Lista todos os alvos disponíveis
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*##"}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
