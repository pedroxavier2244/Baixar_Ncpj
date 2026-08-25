"""
Job socio_empresas — empresas irmãs (mesmo dono) por CNPJ da carteira.

Substitui o enriquecimento que o motor fazia dentro do sense.py::enrich_socios,
que batia CNPJ a CNPJ na API e escrevia o CSV 29-socio-empresas.csv. Lá a
cobertura era ~27% porque só as linhas da foto do dia eram enriquecidas; aqui a
base inteira é varrida e a linha para de envelhecer.

    lista de CNPJs (data_base, Supabase do CRM)
        -> socio.calcular_lote() no cnpj_db (migration 009)
        -> upsert em socio.socio_empresas + socio.socio_empresas_fetch_log

POR QUE O CÁLCULO É SQL E NÃO HTTP
A issue original descrevia o job batendo em /cnpj/{cnpj}/socios e /socios/buscar,
que é o que o motor fazia por não ter o banco à mão. Aqui dentro rf_socios está
na mesa ao lado: o reverso vira um JOIN. Some a fila, o timeout e o risco de
derrubar a própria API — que era exatamente o que a issue pedia para respeitar.
Medido em 24/08/2026: 22.141 CNPJs em 3min37s.

DEPENDE DO ÍNDICE idx_rf_socios_new_cpf_nome. Sem ele o reverso vira seq scan
sobre 28M linhas (3,1s por consulta, contra 8,9ms com índice) e este job passa de
minutos para horas. Ele é criado pelo index_sqls do steps/load_step.py; a
checagem abaixo falha cedo e explica, em vez de deixar o job arrastar em silêncio.

Usage:
    python socio_empresas_job.py            # respeita a janela horária
    python socio_empresas_job.py --agora    # roda uma vez e sai
    python socio_empresas_job.py --dry-run  # só relata o que faria
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.parse
import urllib.request
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import psycopg

from config import settings
from logger import get_logger

log = get_logger("socio_empresas_job")

_INDICE_REVERSO = "idx_rf_socios_new_cpf_nome"
_PAGINA_SUPABASE = 1000


# ── Lista de CNPJs (lado CRM) ───────────────────────────────────────────────

def buscar_cnpjs_da_base() -> list[str]:
    """
    Lê a carteira do Supabase, paginando. Devolve CNPJs de 14 dígitos.

    O LPAD não é zelo: a data_base entrega CNPJ sem zero à esquerda (1.081 de
    22.141 medidos em 24/08/2026, ou 4,9%). Sem preencher, eles não casam com
    nada na Receita e sumiriam do resultado sem erro nenhum.
    """
    if not settings.supabase_url or not settings.supabase_key:
        raise RuntimeError(
            "SUPABASE_URL/SUPABASE_KEY não configuradas — sem elas o job não "
            "sabe QUAIS CNPJs processar (a carteira mora no Supabase do CRM)"
        )

    base = settings.supabase_url.rstrip("/")
    coluna = settings.supabase_base_coluna
    vistos: set[str] = set()
    offset = 0

    while True:
        qs = urllib.parse.urlencode({
            "select": coluna,
            "order":  "id",
            "offset": offset,
            "limit":  _PAGINA_SUPABASE,
        })
        url = f"{base}/rest/v1/{settings.supabase_base_table}?{qs}"
        req = urllib.request.Request(url, headers={
            "apikey":        settings.supabase_key,
            "Authorization": f"Bearer {settings.supabase_key}",
        })
        with urllib.request.urlopen(req, timeout=60) as resp:
            linhas = json.loads(resp.read().decode("utf-8"))

        if not linhas:
            break

        for linha in linhas:
            digitos = "".join(ch for ch in str(linha.get(coluna) or "") if ch.isdigit())
            if 11 <= len(digitos) <= 14:
                vistos.add(digitos.zfill(14))

        offset += _PAGINA_SUPABASE
        if len(linhas) < _PAGINA_SUPABASE:
            break

    log.info(f"carteira lida: {len(vistos):,} CNPJs distintos")
    return sorted(vistos)


# ── Cálculo (lado VPS) ──────────────────────────────────────────────────────

def conferir_indice(conn: psycopg.Connection) -> None:
    """Falha cedo se o índice do reverso não estiver lá — ver docstring do módulo."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_indexes "
            " WHERE schemaname = %s AND tablename = 'rf_socios' AND indexname = %s",
            (settings.pg_schema, _INDICE_REVERSO),
        )
        if cur.fetchone() is None:
            raise RuntimeError(
                f"índice {_INDICE_REVERSO} não existe em {settings.pg_schema}.rf_socios. "
                "Sem ele o reverso vira seq scan sobre 28M linhas e este job leva horas. "
                "Recriar com db/migrations/006_socios_buscar_index.sql. "
                "Se ele sumiu depois de uma carga da RF, o index_sqls do "
                "steps/load_step.py está incompleto."
            )


def carga_rf_em_andamento(conn: psycopg.Connection) -> bool:
    """
    True enquanto o ETL da Receita está reconstruindo as tabelas.

    O sinal é a existência das tabelas _new: elas só existem entre o início do
    build e o swap. Não depende do SQLite de controle nem de volume montado.

    POR QUE NÃO RODAR JUNTO (a carga leva 4-5h, começa às 3h):
      * o load_step DROPA o índice do reverso da tabela VIVA antes de recriá-lo
        na _new — no meio da carga o reverso é seq scan sobre 28M linhas e este
        job passa de minutos para horas;
      * o swap final é ALTER TABLE ... RENAME, que precisa de ACCESS EXCLUSIVE.
        Uma query longa daqui faria o ALTER esperar: o job atrasaria a CARGA.

    Pular custa nada — o dado da RF é mensal e a próxima noite pega tudo.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass(%s), to_regclass(%s)",
            (f"{settings.pg_schema}.rf_socios_new",
             f"{settings.pg_schema}.rf_estabelecimentos_new"),
        )
        socios_new, estab_new = cur.fetchone()
    return socios_new is not None or estab_new is not None


def run_key_atual(conn: psycopg.Connection) -> str | None:
    """run_key da carga viva. Uma linha qualquer serve — é igual na tabela toda."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT run_key FROM {settings.pg_schema}.rf_socios LIMIT 1")
        linha = cur.fetchone()
    return linha[0].strip() if linha and linha[0] else None


def ler_controle(conn: psycopg.Connection, chave: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT valor FROM {settings.pg_socio_schema}.job_controle WHERE chave = %s",
            (chave,),
        )
        linha = cur.fetchone()
    return linha[0] if linha else None


def gravar_controle(conn: psycopg.Connection, chave: str, valor: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {settings.pg_socio_schema}.job_controle (chave, valor, atualizado_em) "
            "VALUES (%s, %s, NOW()) "
            "ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor, "
            "atualizado_em = EXCLUDED.atualizado_em",
            (chave, valor),
        )


def processar_lote(conn: psycopg.Connection, lote: list[str], dias: int) -> int:
    """Uma chamada de calcular_lote. Devolve quantas linhas voltaram."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*) FROM {settings.pg_socio_schema}.calcular_lote(%s, %s, %s)",
            (lote, dias, settings.socio_job_max_lista),
        )
        return cur.fetchone()[0]


def rodar(dry_run: bool = False) -> dict:
    """
    Uma execução completa. Idempotente: calcular_lote pula linha fresca
    (socio_job_dias), então rodar duas vezes na mesma noite não refaz trabalho
    nem corrompe — só reconfirma.
    """
    inicio = time.monotonic()

    # autocommit=True: cada lote fecha sozinho. Uma transação única sobre 22k
    # CNPJs seguraria a escrita por minutos e perderia TUDO num erro no fim —
    # com lote fechado, uma falha custa só o lote corrente. Também mantém as
    # travas em rf_socios curtas, para não fazer o swap da carga esperar.
    with psycopg.connect(settings.postgres_url, autocommit=True) as conn:
        # Antes de qualquer coisa: a carga da RF está rodando? Ver docstring de
        # carga_rf_em_andamento — rodar junto degrada este job E atrasa a carga.
        if carga_rf_em_andamento(conn):
            log.warning(
                "carga da RF em andamento (tabelas _new existem) - adiado. "
                "Se isto se repetir por dias, pode ser _new órfã de uma carga "
                "que falhou; conferir antes de assumir que a carga está viva."
            )
            return {"status": "adiado", "motivo": "carga_rf_em_andamento"}

        conferir_indice(conn)

        # Carga nova = todo o dado de sócio mudou de uma vez. As linhas foram
        # calculadas há pouco e o p_dias=45 as consideraria frescas, deixando o
        # dado novo de fora por mais de um mês. Quando o run_key muda, ignora a
        # janela de frescor e recalcula tudo.
        rk_agora = run_key_atual(conn)
        rk_antes = ler_controle(conn, "ultimo_run_key")
        recarga = bool(rk_agora and rk_agora != rk_antes)
        dias = 0 if recarga else settings.socio_job_dias
        if recarga:
            log.info(
                f"run_key mudou ({rk_antes or 'nenhum'} -> {rk_agora}): "
                "carga nova da RF, recalculando a base inteira"
            )

        cnpjs = buscar_cnpjs_da_base()

        if dry_run:
            n_lotes = (len(cnpjs) + settings.socio_job_lote - 1) // settings.socio_job_lote
            log.info(
                f"[dry-run] {len(cnpjs):,} CNPJs em {n_lotes} lotes, "
                f"p_dias={dias}{' (recálculo total)' if recarga else ''}"
            )
            return {"status": "dry_run", "cnpjs": len(cnpjs),
                    "lotes": n_lotes, "dias": dias, "recarga": recarga}

        total = 0
        falhas = 0
        lotes = [
            cnpjs[i:i + settings.socio_job_lote]
            for i in range(0, len(cnpjs), settings.socio_job_lote)
        ]
        for n, lote in enumerate(lotes, 1):
            # Recheca a cada lote: a carga pode começar no meio da execução.
            # Sair aqui deixa o que já foi commitado de pé e o fetch_log mostra
            # até onde chegou — nada se perde, a próxima noite continua.
            if carga_rf_em_andamento(conn):
                log.warning(
                    f"carga da RF começou durante a execução - parando no lote "
                    f"{n}/{len(lotes)} ({total:,} linhas já gravadas)"
                )
                falhas += 1
                break
            try:
                total += processar_lote(conn, lote, dias)
                log.info(f"lote {n}/{len(lotes)} — {total:,}/{len(cnpjs):,}")
            except Exception as exc:
                # Um lote ruim não derruba a noite inteira: o que já entrou está
                # commitado, e o fetch_log mostra quem ficou de fora.
                falhas += 1
                log.error(f"lote {n}/{len(lotes)} falhou: {exc}")

        # Só marca o run_key numa execução limpa. Marcar um recálculo pela metade
        # faria o job achar que já absorveu a carga nova, e o resto ficaria velho
        # em silêncio até a próxima carga.
        if not falhas and rk_agora:
            gravar_controle(conn, "ultimo_run_key", rk_agora)

    dur = time.monotonic() - inicio
    log.info(
        f"fim — {total:,} linhas, {falhas} lote(s) com falha, {dur:.0f}s"
    )
    return {"status": "ok" if not falhas else "parcial",
            "linhas": total, "lotes_com_falha": falhas,
            "recarga": recarga, "segundos": round(dur)}


# ── Agendamento ─────────────────────────────────────────────────────────────

def deve_rodar(agora: datetime, hora: int, ultima: date | None) -> bool:
    """True quando já passou da hora-alvo e ainda não rodamos hoje."""
    return agora.hour >= hora and ultima != agora.date()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agora", action="store_true", help="roda uma vez e sai")
    ap.add_argument("--dry-run", action="store_true", help="só relata o que faria")
    args = ap.parse_args()

    if args.agora or args.dry_run:
        # Traceback aqui não ajuda ninguém: quem roda na mão quer a linha que
        # diz o que fazer. O erro já vem escrito para ser lido sozinho.
        try:
            resultado = rodar(dry_run=args.dry_run)
        except Exception as exc:
            log.error(str(exc))
            sys.exit(1)
        print(json.dumps(resultado, ensure_ascii=False))
        sys.exit(0 if resultado["status"] in ("ok", "dry_run") else 1)

    if not settings.socio_job_enabled:
        log.warning("job desabilitado (SOCIO_JOB_ENABLED=false) - ocioso")
        while True:
            time.sleep(3600)

    tz = ZoneInfo(settings.socio_job_tz)
    log.info(
        f"job socio_empresas iniciado - execução diária às "
        f"{settings.socio_job_hour:02d}h ({settings.socio_job_tz}), "
        f"poll a cada {settings.socio_job_poll_seconds}s"
    )
    ultima: date | None = None

    while True:
        try:
            agora = datetime.now(tz)
            if deve_rodar(agora, settings.socio_job_hour, ultima):
                log.info(f"janela atingida ({agora:%H:%M %Z})")
                resultado = rodar()
                if resultado["status"] == "ok":
                    ultima = agora.date()
                elif resultado["status"] == "adiado":
                    # Carga da RF rodando. Não marca o dia: o próximo poll (30min)
                    # tenta de novo, e quando a carga terminar o job pega o dado
                    # novo — inclusive com o recálculo total do run_key.
                    log.info("adiado - tenta de novo no próximo poll")
                else:
                    # Não marca o dia: retenta no próximo poll. Parcial que se
                    # marca como feito vira buraco silencioso na cobertura.
                    log.error(f"execução parcial: {resultado} - retenta")
        except Exception as exc:
            log.error(f"erro no ciclo: {exc} - retenta no próximo poll")
        time.sleep(settings.socio_job_poll_seconds)


if __name__ == "__main__":
    main()
