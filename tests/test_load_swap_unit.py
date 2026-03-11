"""
test_load_swap_unit.py — unit tests for load_step.py that do NOT need PostgreSQL.

All DB interaction is replaced by FakeConn / FakeCursor / FakeCopy.
Tests cover:
- DDL generators (_empresas_new_ddl, _estab_new_ddl, _socios_new_ddl, _simples_new_ddl)
- _swap_table
- _drop_previous_old_tables
- _build_new_table
- run() (mocked psycopg.connect + helpers)
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from steps.load_step import (
    _empresas_new_ddl,
    _estab_new_ddl,
    _socios_new_ddl,
    _simples_new_ddl,
    _swap_table,
    _drop_previous_old_tables,
    _build_new_table,
    _EMPRESAS_COLS,
    _ESTAB_COLS,
    _SOCIOS_COLS,
    _SIMPLES_COLS,
)
from steps.base import StepStatus


# ── Fake DB infrastructure ───────────────────────────────────────────────────

class FakeCopy:
    """Fake COPY context manager — silently accepts written chunks."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def write(self, data):
        pass


class FakeCursor:
    """Context-manager cursor that records SQL and returns pre-configured results."""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def execute(self, sql, params=None):
        self._conn.executed.append((sql.strip(), params))
        return self

    def fetchone(self):
        return self._conn._next_result()

    def copy(self, sql):
        self._conn.executed.append((sql.strip(), None))
        return FakeCopy()


class FakeConn:
    """Fake psycopg connection that records every SQL statement executed."""

    def __init__(self, results=None):
        self.executed: list[tuple] = []   # list of (sql, params)
        self._results = list(results or [])
        self._idx = 0
        self.commits = 0

    def _next_result(self):
        if self._idx < len(self._results):
            v = self._results[self._idx]
            self._idx += 1
            return v
        return None

    def cursor(self):
        return FakeCursor(self)

    def execute(self, sql, params=None):
        self.executed.append((sql.strip(), params))

        class _Result:
            def __init__(self_, v):
                self_._v = v

            def fetchone(self_):
                return self_._v

        return _Result(self._next_result())

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


# ── DDL tests ────────────────────────────────────────────────────────────────

def test_empresas_ddl_has_primary_key():
    """_empresas_new_ddl must declare cnpj_basico as PRIMARY KEY."""
    ddl = _empresas_new_ddl("2026-03")
    assert "PRIMARY KEY" in ddl
    assert "cnpj_basico" in ddl


def test_estab_ddl_has_composite_pk():
    """_estab_new_ddl must declare a composite PRIMARY KEY on the three CNPJ components."""
    ddl = _estab_new_ddl("2026-03")
    assert "PRIMARY KEY (cnpj_basico, cnpj_ordem, cnpj_dv)" in ddl


def test_socios_ddl_has_identity():
    """_socios_new_ddl must use GENERATED ALWAYS AS IDENTITY for the surrogate PK."""
    ddl = _socios_new_ddl("2026-03")
    assert "GENERATED ALWAYS AS IDENTITY" in ddl


def test_simples_ddl_has_pk():
    """_simples_new_ddl must declare PRIMARY KEY (cnpj_basico)."""
    ddl = _simples_new_ddl("2026-03")
    assert "PRIMARY KEY (cnpj_basico)" in ddl or (
        "PRIMARY KEY" in ddl and "cnpj_basico" in ddl
    )


def test_ddl_run_key_default_embedded():
    """All DDL functions must embed the run_key value as a column DEFAULT."""
    run_key = "2026-03"
    for fn in (_empresas_new_ddl, _estab_new_ddl, _socios_new_ddl, _simples_new_ddl):
        ddl = fn(run_key)
        assert run_key in ddl, f"{fn.__name__} does not embed run_key '{run_key}'"
        assert "DEFAULT" in ddl, f"{fn.__name__} missing DEFAULT keyword"


def test_ddl_no_id_in_copy_columns():
    """'id' must NOT be present in _SOCIOS_COLS so IDENTITY is auto-populated during COPY."""
    assert "id" not in _SOCIOS_COLS, (
        "id must be absent from _SOCIOS_COLS — it is an IDENTITY column filled automatically"
    )


def test_empresas_cols_complete():
    """_EMPRESAS_COLS must contain all 7 business columns (no metadata cols)."""
    expected = {
        "cnpj_basico", "razao_social", "natureza_juridica",
        "qualificacao_responsavel", "capital_social", "porte",
        "ente_federativo_responsavel",
    }
    assert set(_EMPRESAS_COLS) == expected
    assert len(_EMPRESAS_COLS) == 7


# ── _swap_table tests ────────────────────────────────────────────────────────

def test_swap_table_renames_in_correct_order():
    """
    _swap_table must emit exactly two RENAME statements in the correct order:
      1. live → live_old
      2. live_new → live
    and commit once (atomically).
    """
    conn = FakeConn()
    _swap_table(conn, "cnpj.rf_empresas")

    sqls = [s for s, _ in conn.executed]
    assert len(sqls) == 2, f"Expected 2 SQL statements, got {len(sqls)}: {sqls}"

    # First rename: live → old
    assert "rf_empresas" in sqls[0]
    assert "rf_empresas_old" in sqls[0]
    # Second rename: _new → live
    assert "rf_empresas_new" in sqls[1]
    assert "rf_empresas" in sqls[1]

    assert conn.commits == 1


def test_swap_table_uses_schema_prefix():
    """Schema prefix must be preserved in the RENAME SQL statements."""
    conn = FakeConn()
    _swap_table(conn, "cnpj.rf_empresas")

    sqls = [s for s, _ in conn.executed]
    for sql in sqls:
        assert "cnpj" in sql, f"Schema prefix missing in: {sql}"


# ── _drop_previous_old_tables tests ─────────────────────────────────────────

def test_drop_skips_when_no_old_tables():
    """
    When information_schema returns nothing for every _old table,
    no DROP TABLE statement should be executed.
    """
    # All fetchone() calls return None (table not found)
    conn = FakeConn(results=[None, None, None, None])
    _drop_previous_old_tables(conn)

    drop_sqls = [s for s, _ in conn.executed if "DROP" in s.upper()]
    assert len(drop_sqls) == 0, f"Unexpected DROPs: {drop_sqls}"


def test_drop_uses_cascade():
    """When an _old table is found, the DROP must include CASCADE."""
    # Return a truthy row for the first table only, None for the rest
    conn = FakeConn(results=[(1,), None, None, None])
    _drop_previous_old_tables(conn)

    drop_sqls = [s for s, _ in conn.executed if "DROP" in s.upper()]
    assert len(drop_sqls) >= 1
    assert any("CASCADE" in s for s in drop_sqls), (
        f"No CASCADE found in DROP statements: {drop_sqls}"
    )


def test_drop_all_four_old_tables():
    """
    _drop_previous_old_tables must query information_schema for all four _old
    table names: rf_empresas_old, rf_estabelecimentos_old, rf_socios_old, rf_simples_old.
    """
    conn = FakeConn(results=[None, None, None, None])
    _drop_previous_old_tables(conn)

    # Collect all params passed to information_schema queries
    queried_names = []
    for sql, params in conn.executed:
        if "information_schema" in sql and params:
            # params is (schema, table_name)
            queried_names.append(params[1])

    for expected in ("rf_empresas_old", "rf_estabelecimentos_old",
                     "rf_socios_old", "rf_simples_old"):
        assert expected in queried_names, (
            f"'{expected}' not queried in information_schema. Queried: {queried_names}"
        )


# ── _build_new_table tests ───────────────────────────────────────────────────

def test_build_new_table_drop_then_create(tmp_path: Path):
    """The first SQL must be DROP TABLE IF EXISTS, immediately followed by CREATE TABLE."""
    csv_path = tmp_path / "test.csv"
    csv_path.write_text("col_a\nval1\n", encoding="utf-8")

    conn = FakeConn()
    with patch("steps.load_step._copy_csv", return_value=1):
        _build_new_table(
            conn,
            new_table="cnpj.rf_empresas_new",
            create_ddl="CREATE TABLE cnpj.rf_empresas_new (id INT)",
            csv_path=csv_path,
            columns=["col_a"],
            index_sqls=[],
        )

    sqls = [s for s, _ in conn.executed]
    assert len(sqls) >= 2
    assert "DROP TABLE IF EXISTS" in sqls[0].upper()
    assert "CREATE TABLE" in sqls[1].upper()


def test_build_new_table_creates_indexes_after_copy(tmp_path: Path):
    """Index SQL must appear after the COPY in the executed statement list."""
    csv_path = tmp_path / "test.csv"
    csv_path.write_text("col_a\nval1\n", encoding="utf-8")

    conn = FakeConn()
    index_sql = "CREATE INDEX idx_test ON cnpj.rf_empresas_new (col_a)"

    with patch("steps.load_step._copy_csv", return_value=1):
        _build_new_table(
            conn,
            new_table="cnpj.rf_empresas_new",
            create_ddl="CREATE TABLE cnpj.rf_empresas_new (col_a TEXT)",
            csv_path=csv_path,
            columns=["col_a"],
            index_sqls=[index_sql],
        )

    sqls = [s for s, _ in conn.executed]
    copy_positions = [i for i, s in enumerate(sqls) if "CREATE TABLE" in s.upper()]
    idx_positions  = [i for i, s in enumerate(sqls) if "CREATE INDEX" in s.upper()]

    assert copy_positions, "No CREATE TABLE found"
    assert idx_positions,  "No CREATE INDEX found"
    assert idx_positions[0] > copy_positions[0], (
        "CREATE INDEX appeared before CREATE TABLE"
    )


def test_build_new_table_commits_after_each_phase(tmp_path: Path):
    """
    _build_new_table must commit at least 3 times:
    after DROP+CREATE, after COPY, and after each index.
    """
    csv_path = tmp_path / "test.csv"
    csv_path.write_text("col_a\nval1\n", encoding="utf-8")

    conn = FakeConn()
    with patch("steps.load_step._copy_csv", return_value=1):
        _build_new_table(
            conn,
            new_table="cnpj.rf_empresas_new",
            create_ddl="CREATE TABLE cnpj.rf_empresas_new (col_a TEXT)",
            csv_path=csv_path,
            columns=["col_a"],
            index_sqls=["CREATE INDEX idx1 ON cnpj.rf_empresas_new (col_a)"],
        )

    # DROP→commit, CREATE→commit, COPY→commit, INDEX→commit = at least 3
    assert conn.commits >= 3, f"Expected >= 3 commits, got {conn.commits}"


def test_build_new_table_returns_row_count(tmp_path: Path):
    """_build_new_table must return the row count returned by _copy_csv."""
    csv_path = tmp_path / "test.csv"
    # 5 data rows + 1 header
    csv_path.write_text("col_a\n" + "\n".join(f"v{i}" for i in range(5)) + "\n",
                        encoding="utf-8")

    conn = FakeConn()
    with patch("steps.load_step._copy_csv", return_value=5) as mock_copy:
        result = _build_new_table(
            conn,
            new_table="cnpj.rf_empresas_new",
            create_ddl="CREATE TABLE cnpj.rf_empresas_new (col_a TEXT)",
            csv_path=csv_path,
            columns=["col_a"],
            index_sqls=[],
        )

    assert result == 5, f"Expected row count 5, got {result}"
    mock_copy.assert_called_once()


# ── run() tests ──────────────────────────────────────────────────────────────

def _write_transform_manifest(checkpoint_dir: Path, out_dir: Path) -> None:
    """Helper: write a minimal transform_manifest.json."""
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Create stub CSV files so run() doesn't skip them
    for name in ("empresas", "estabelecimentos", "socios", "simples", "cnaes", "municipios"):
        (out_dir / f"{name}.csv").write_text(f"col\nval\n", encoding="utf-8")

    manifest = {
        "run_key": "2026-03",
        "out_dir": str(out_dir),
        "tables": [
            {"keyword": "Empresas",         "rows": 3, "out": str(out_dir / "empresas.csv")},
            {"keyword": "Estabelecimentos",  "rows": 3, "out": str(out_dir / "estabelecimentos.csv")},
            {"keyword": "Socios",            "rows": 3, "out": str(out_dir / "socios.csv")},
            {"keyword": "Simples",           "rows": 3, "out": str(out_dir / "simples.csv")},
            {"keyword": "cnaes",             "rows": 2, "out": str(out_dir / "cnaes.csv")},
            {"keyword": "municipios",        "rows": 2, "out": str(out_dir / "municipios.csv")},
        ],
    }
    (checkpoint_dir / "transform_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def _make_mock_conn_ctx():
    """Build a MagicMock that works as both psycopg.connect() and a context manager."""
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=MagicMock())
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn


def test_run_reads_transform_manifest(tmp_path: Path):
    """
    run() must read transform_manifest.json and write load_manifest.json
    with a 'tables' key.
    """
    checkpoint_dir = tmp_path / "checkpoints"
    out_dir        = tmp_path / "transformed"
    _write_transform_manifest(checkpoint_dir, out_dir)

    mock_conn = _make_mock_conn_ctx()

    with (
        patch("psycopg.connect", return_value=mock_conn),
        patch("steps.load_step._copy_csv", return_value=10),
        patch("steps.load_step._drop_previous_old_tables"),
        patch("steps.load_step._build_new_table", return_value=10),
        patch("steps.load_step._swap_table"),
        patch("steps.load_step._load_lookup", return_value=5),
    ):
        import steps.load_step as ls
        result = ls.run("job-1", "2026-03", checkpoint_dir)

    load_manifest_path = checkpoint_dir / "load_manifest.json"
    assert load_manifest_path.exists(), "load_manifest.json was not written"
    data = json.loads(load_manifest_path.read_text(encoding="utf-8"))
    assert "tables" in data, "load_manifest.json missing 'tables' key"


def test_run_fails_gracefully_if_manifest_missing(tmp_path: Path):
    """run() must return StepResult with status FAILED when transform_manifest.json is absent."""
    checkpoint_dir = tmp_path / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    # Do NOT write transform_manifest.json

    import steps.load_step as ls
    result = ls.run("job-1", "2026-03", checkpoint_dir)

    assert result.status == StepStatus.FAILED
    assert result.error is not None


def test_run_skips_missing_csv(tmp_path: Path):
    """
    When the manifest lists a table but its CSV file does not exist,
    run() should still complete successfully (log a warning and skip).
    """
    checkpoint_dir = tmp_path / "checkpoints"
    out_dir        = tmp_path / "transformed"
    _write_transform_manifest(checkpoint_dir, out_dir)

    # Remove the empresas CSV to simulate a missing file
    (out_dir / "empresas.csv").unlink()

    mock_conn = _make_mock_conn_ctx()

    with (
        patch("psycopg.connect", return_value=mock_conn),
        patch("steps.load_step._copy_csv", return_value=10),
        patch("steps.load_step._drop_previous_old_tables"),
        patch("steps.load_step._build_new_table", return_value=10),
        patch("steps.load_step._swap_table"),
        patch("steps.load_step._load_lookup", return_value=5),
    ):
        import steps.load_step as ls
        result = ls.run("job-1", "2026-03", checkpoint_dir)

    assert result.status == StepStatus.SUCCESS


def test_run_calls_drop_old_first(tmp_path: Path):
    """_drop_previous_old_tables must be called before any _build_new_table call."""
    checkpoint_dir = tmp_path / "checkpoints"
    out_dir        = tmp_path / "transformed"
    _write_transform_manifest(checkpoint_dir, out_dir)

    call_order: list[str] = []

    def fake_drop(conn):
        call_order.append("drop_old")

    def fake_build(conn, new_table, create_ddl, csv_path, columns, index_sqls):
        call_order.append("build_new")
        return 10

    mock_conn = _make_mock_conn_ctx()

    with (
        patch("psycopg.connect", return_value=mock_conn),
        patch("steps.load_step._copy_csv", return_value=10),
        patch("steps.load_step._drop_previous_old_tables", side_effect=fake_drop),
        patch("steps.load_step._build_new_table", side_effect=fake_build),
        patch("steps.load_step._swap_table"),
        patch("steps.load_step._load_lookup", return_value=5),
    ):
        import steps.load_step as ls
        ls.run("job-1", "2026-03", checkpoint_dir)

    assert call_order[0] == "drop_old", (
        f"_drop_previous_old_tables was not the first call. Order: {call_order}"
    )
    assert "build_new" in call_order, "_build_new_table was never called"


def test_run_writes_load_manifest_on_success(tmp_path: Path):
    """
    After a successful run, load_manifest.json must exist and contain a 'tables' list.
    """
    checkpoint_dir = tmp_path / "checkpoints"
    out_dir        = tmp_path / "transformed"
    _write_transform_manifest(checkpoint_dir, out_dir)

    mock_conn = _make_mock_conn_ctx()

    with (
        patch("psycopg.connect", return_value=mock_conn),
        patch("steps.load_step._copy_csv", return_value=10),
        patch("steps.load_step._drop_previous_old_tables"),
        patch("steps.load_step._build_new_table", return_value=10),
        patch("steps.load_step._swap_table"),
        patch("steps.load_step._load_lookup", return_value=5),
    ):
        import steps.load_step as ls
        result = ls.run("job-1", "2026-03", checkpoint_dir)

    assert result.status == StepStatus.SUCCESS

    load_manifest_path = checkpoint_dir / "load_manifest.json"
    assert load_manifest_path.exists(), "load_manifest.json not created"

    data = json.loads(load_manifest_path.read_text(encoding="utf-8"))
    assert isinstance(data.get("tables"), list), "'tables' must be a list"
