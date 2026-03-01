# Task Scheduler — Instrucoes

## Pre-requisitos
- Python 3.11+ instalado
- PowerShell como Administrador
- Projeto configurado com `.env` valido

## Instalar as tarefas

```powershell
# Abra PowerShell como Administrador
powershell -ExecutionPolicy Bypass -File scheduler\install_tasks.ps1
```

## Tarefas criadas

| Tarefa | Gatilho | Descricao |
|---|---|---|
| `ETL_CNPJ_Enqueue` | Diario as 06:00 | Verifica novo mes e enfileira job |
| `ETL_CNPJ_Worker` | Inicializacao do servidor | Worker processa jobs da fila |
| `ETL_CNPJ_API` | Inicializacao do servidor | API FastAPI na porta 8000 |

## Iniciar manualmente

```powershell
Start-ScheduledTask -TaskName "ETL_CNPJ_Worker"
Start-ScheduledTask -TaskName "ETL_CNPJ_API"
Start-ScheduledTask -TaskName "ETL_CNPJ_Enqueue"
```

## Ver logs

```
logs\etl_YYYY-MM-DD.log
```

## Executar primeiro run manualmente

```bash
# No prompt com .venv ativado
python enqueue_job.py --run-key 2026-02
python worker.py --once
```
