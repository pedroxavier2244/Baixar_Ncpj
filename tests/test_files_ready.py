"""
Guardas dos modos sem rede do enqueue_job: `--status` e `--files-ready`.

Existem porque o download saiu da VPS em 11/09/2026 — ela não alcança mais a
Receita. Quem baixa é o Mac mini, e o `--files-ready` é o único caminho pelo
qual um job passa a existir a partir de arquivos entregues de fora.

O cenário que estes testes cobrem de perto é o arquivo truncado: o
`download_step` pula qualquer arquivo com tamanho maior que zero, então um ZIP
pela metade entra no pipeline e só barra horas depois, no verify_step.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

RUN_KEY = "2026-10"


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


def _preparar(tmp_path, tamanhos: dict[str, int], *, escrever: dict[str, int] | None = None,
              run_key: str = RUN_KEY, com_manifest: bool = True):
    """
    Escreve o manifest com `tamanhos` e os arquivos com `escrever`
    (default: iguais ao manifest — o caminho feliz).
    """
    ck = tmp_path / "checkpoints"
    ck.mkdir(parents=True, exist_ok=True)
    d = tmp_path / "data" / run_key
    d.mkdir(parents=True, exist_ok=True)

    if com_manifest:
        files = [
            {"name": nome, "path": nome, "is_dir": False,
             "size": tam, "modified": "", "etag": f"etag-{nome}"}
            for nome, tam in tamanhos.items()
        ]
        (ck / f"webdav_manifest_{run_key}.json").write_text(
            json.dumps({"run_key": run_key, "files": files}, indent=2), encoding="utf-8"
        )

    for nome, tam in (escrever if escrever is not None else tamanhos).items():
        (d / nome).write_bytes(b"x" * tam)

    return d


# ── --files-ready ─────────────────────────────────────────────────────────────

def test_caminho_feliz_cria_o_job(tmp_path):
    _preparar(tmp_path, {"Empresas0.zip": 100, "Socios0.zip": 50})
    with _settings_patch(tmp_path):
        from enqueue_job import files_ready
        r = files_ready(RUN_KEY)
        assert r["status"] == "created"
        assert r["files"] == 2
        assert r["bytes"] == 150
        assert r["job_id"]


def test_arquivo_faltando_recusa(tmp_path):
    _preparar(tmp_path, {"Empresas0.zip": 100, "Socios0.zip": 50},
              escrever={"Empresas0.zip": 100})
    with _settings_patch(tmp_path):
        from enqueue_job import files_ready
        r = files_ready(RUN_KEY)
        assert r["status"] == "files_mismatch"
        assert r["problemas"] == ["falta Socios0.zip"]


def test_arquivo_truncado_recusa(tmp_path):
    """O caso que motivou a conferência: tamanho menor que o do manifest."""
    _preparar(tmp_path, {"Empresas0.zip": 100},
              escrever={"Empresas0.zip": 60})
    with _settings_patch(tmp_path):
        from enqueue_job import files_ready
        r = files_ready(RUN_KEY)
        assert r["status"] == "files_mismatch"
        assert r["problemas"] == ["tamanho Empresas0.zip: 60 != 100"]


def test_nenhum_job_criado_quando_recusa(tmp_path):
    _preparar(tmp_path, {"Empresas0.zip": 100}, escrever={})
    with _settings_patch(tmp_path):
        from db.control import get_job_by_run_key, init_db
        from enqueue_job import files_ready
        files_ready(RUN_KEY)
        init_db()
        assert get_job_by_run_key(RUN_KEY) is None


def test_job_ja_existente_recusa(tmp_path):
    _preparar(tmp_path, {"Empresas0.zip": 100})
    with _settings_patch(tmp_path):
        from enqueue_job import files_ready
        primeiro = files_ready(RUN_KEY)
        assert primeiro["status"] == "created"
        segundo = files_ready(RUN_KEY)
        assert segundo["status"] == "job_exists"
        assert segundo["job_id"] == primeiro["job_id"]


def test_job_dead_tambem_recusa(tmp_path):
    """Reprocessar um DEAD é decisão manual — o coletor não pode forçar."""
    _preparar(tmp_path, {"Empresas0.zip": 100})
    with _settings_patch(tmp_path):
        from db.control import finish_job, get_conn
        from enqueue_job import files_ready
        r = files_ready(RUN_KEY)
        with get_conn() as conn:
            conn.execute("UPDATE job_queue SET attempts = max_attempts WHERE job_id = ?",
                         (r["job_id"],))
        finish_job(r["job_id"], success=False, error="boom")
        de_novo = files_ready(RUN_KEY)
        assert de_novo["status"] == "job_exists"
        assert de_novo["job_status"] == "DEAD"


def test_manifest_ausente_recusa(tmp_path):
    _preparar(tmp_path, {"Empresas0.zip": 100}, com_manifest=False)
    with _settings_patch(tmp_path):
        from enqueue_job import files_ready
        r = files_ready(RUN_KEY)
        assert r["status"] == "manifest_missing"


def test_dry_run_nao_cria_job(tmp_path):
    _preparar(tmp_path, {"Empresas0.zip": 100})
    with _settings_patch(tmp_path):
        from db.control import get_job_by_run_key, init_db
        from enqueue_job import files_ready
        r = files_ready(RUN_KEY, dry_run=True)
        assert r["status"] == "dry_run_ok"
        init_db()
        assert get_job_by_run_key(RUN_KEY) is None


def test_arquivo_fora_do_manifest_nao_impede(tmp_path):
    """
    extract_step itera o manifest, nunca a pasta — um resto de `.part` não
    quebra o pipeline, então é aviso e não recusa.
    """
    d = _preparar(tmp_path, {"Empresas0.zip": 100})
    (d / "Empresas0.zip.part").write_bytes(b"y" * 7)
    with _settings_patch(tmp_path):
        from enqueue_job import files_ready
        r = files_ready(RUN_KEY)
        assert r["status"] == "created"


def test_files_ready_nao_toca_a_rede(tmp_path):
    """
    A razão de existir do modo: na VPS qualquer chamada à Receita falha. Se o
    caminho chamasse o PROPFIND, este teste explodiria.
    """
    _preparar(tmp_path, {"Empresas0.zip": 100})

    def boom(*a, **k):
        raise AssertionError("files_ready nao pode falar com a Receita")

    with _settings_patch(tmp_path), patch("enqueue_job.propfind_listing", side_effect=boom):
        from enqueue_job import files_ready
        assert files_ready(RUN_KEY)["status"] == "created"


# ── --status ──────────────────────────────────────────────────────────────────

def test_status_sem_job_devolve_none(tmp_path):
    with _settings_patch(tmp_path):
        from enqueue_job import job_status
        assert job_status(RUN_KEY) is None


def test_status_traz_o_job(tmp_path):
    _preparar(tmp_path, {"Empresas0.zip": 100})
    with _settings_patch(tmp_path):
        from enqueue_job import files_ready, job_status
        criado = files_ready(RUN_KEY)
        st = job_status(RUN_KEY)
        assert st["status"] == "PENDING"
        assert st["job_id"] == criado["job_id"]
        assert st["attempts"] == 0
        assert st["run_key"] == RUN_KEY


def test_status_reflete_success(tmp_path):
    """É por este campo que o coletor decide apagar os ZIPs locais."""
    _preparar(tmp_path, {"Empresas0.zip": 100})
    with _settings_patch(tmp_path):
        from db.control import finish_job
        from enqueue_job import files_ready, job_status
        r = files_ready(RUN_KEY)
        finish_job(r["job_id"], success=True)
        assert job_status(RUN_KEY)["status"] == "SUCCESS"


def test_status_nao_toca_a_rede(tmp_path):
    def boom(*a, **k):
        raise AssertionError("job_status nao pode falar com a Receita")

    with _settings_patch(tmp_path), patch("enqueue_job.propfind_listing", side_effect=boom):
        from enqueue_job import job_status
        assert job_status(RUN_KEY) is None


# ── check_local_files isolado ─────────────────────────────────────────────────

@pytest.mark.parametrize("tamanho_real,esperado_ok", [(100, True), (99, False), (101, False)])
def test_conferencia_por_tamanho_exato(tmp_path, tamanho_real, esperado_ok):
    _preparar(tmp_path, {"Empresas0.zip": 100}, escrever={"Empresas0.zip": tamanho_real})
    with _settings_patch(tmp_path):
        from enqueue_job import check_local_files
        assert check_local_files(RUN_KEY)["ok"] is esperado_ok
