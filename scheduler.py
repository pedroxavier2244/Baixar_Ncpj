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
