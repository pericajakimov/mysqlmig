"""Integration tests against a real MySQL server (throwaway database).

Marked `integration`; skipped automatically when MySQL is unreachable (see
conftest.mysql_db). They drive the real Runner/Metadata through the mysql client.
"""

import re
import time

import pytest

from mysqlmig import (
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_SUCCESS,
    Config,
    ConnectionConfig,
    Metadata,
    MigrationStateError,
    MysqlClient,
    MysqlmigError,
    Runner,
)

pytestmark = pytest.mark.integration


# --- helpers ----------------------------------------------------------------

def _write(dirpath, name, sql):
    (dirpath / name).write_text(sql, encoding="utf-8")


def _config(database, directory, target=None, verbose=False):
    return Config(
        database=database,
        directory=str(directory),
        connection=ConnectionConfig(database=database),
        target=target,
        verbose=verbose,
    )


def _runner(database, directory, interactive=False, **kw):
    return Runner(_config(database, directory, **kw), interactive=interactive)


def _records(database):
    client = MysqlClient(ConnectionConfig(database=database))
    return Metadata(client).load_records()


def _log(database, version):
    """Read a migration's execution_log directly from the database."""
    client = MysqlClient(ConnectionConfig(database=database))
    rows = client.query(
        "SELECT execution_log FROM schema_version "
        f"WHERE version = '{version}';"
    )
    return rows[0][0] if rows else None


# --- metadata ---------------------------------------------------------------

def test_metadata_created_and_detected(mysql_db):
    client = MysqlClient(ConnectionConfig(database=mysql_db))
    meta = Metadata(client)
    assert meta.exists() is False
    meta.create()
    assert meta.exists() is True
    # Idempotent.
    meta.create()
    assert meta.exists() is True


def test_view_excludes_execution_log(mysql_db):
    client = MysqlClient(ConnectionConfig(database=mysql_db))
    Metadata(client).create()
    cols = client.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'v_schema_version';"
    )
    names = {c[0].lower() for c in cols}
    assert "execution_log" not in names
    assert "execution_start" in names
    # The duration column is named execution_duration (renamed from
    # execution_time) in both the table and the view.
    assert "execution_duration" in names
    assert "execution_time" not in names


# --- lifecycle --------------------------------------------------------------

def test_insert_then_finalize_sets_time(mysql_db):
    client = MysqlClient(ConnectionConfig(database=mysql_db))
    meta = Metadata(client)
    meta.create()
    meta.insert_in_progress("v1.0", "v1.0_initial.sql")

    recs = meta.load_records()
    assert len(recs) == 1
    assert recs[0].status == STATUS_IN_PROGRESS
    assert recs[0].execution_duration is None

    time.sleep(1.1)
    meta.finalize("v1.0", STATUS_SUCCESS, "ok\n")
    recs = meta.load_records()
    assert recs[0].status == STATUS_SUCCESS
    # Duration format HH:MM:SS.mmm (3 decimals), and at least ~1 second elapsed.
    assert recs[0].execution_duration is not None
    assert re.match(r"^\d{2}:\d{2}:\d{2}\.\d{3}$", recs[0].execution_duration)
    assert recs[0].execution_duration.startswith("00:00:01.")


def test_retry_updates_in_place_and_appends_log(mysql_db):
    client = MysqlClient(ConnectionConfig(database=mysql_db))
    meta = Metadata(client)
    meta.create()
    meta.insert_in_progress("v1.0", "v1.0_x.sql")
    meta.finalize("v1.0", STATUS_FAILED, "FIRST\n")

    meta.reset_in_progress("v1.0", "v1.0_x.sql")
    meta.finalize("v1.0", STATUS_SUCCESS, "SECOND\n", append=True)

    recs = meta.load_records()
    # Still exactly one row (updated in place, not duplicated).
    assert len(recs) == 1
    assert recs[0].status == STATUS_SUCCESS
    log = _log(mysql_db, "v1.0")
    assert "FIRST" in log
    assert "SECOND" in log


# --- run command end-to-end -------------------------------------------------

def test_run_applies_all_pending(mysql_db, tmp_path):
    _write(tmp_path, "v1.0_a.sql", "CREATE TABLE a (id INT PRIMARY KEY);")
    _write(tmp_path, "v1.1_b.sql", "CREATE TABLE b (id INT PRIMARY KEY);")
    rc = _runner(mysql_db, tmp_path).run()
    assert rc == 0
    recs = _records(mysql_db)
    assert [str(r.version) for r in recs] == ["v1.0", "v1.1"]
    assert all(r.status == STATUS_SUCCESS for r in recs)


def test_run_to_specific_target(mysql_db, tmp_path):
    _write(tmp_path, "v1.0_a.sql", "CREATE TABLE a (id INT PRIMARY KEY);")
    _write(tmp_path, "v1.1_b.sql", "CREATE TABLE b (id INT PRIMARY KEY);")
    _write(tmp_path, "v1.2_c.sql", "CREATE TABLE c (id INT PRIMARY KEY);")
    rc = _runner(mysql_db, tmp_path, target="v1.1").run()
    assert rc == 0
    recs = _records(mysql_db)
    assert [str(r.version) for r in recs] == ["v1.0", "v1.1"]


def test_run_no_pending_is_success(mysql_db, tmp_path):
    _write(tmp_path, "v1.0_a.sql", "CREATE TABLE a (id INT PRIMARY KEY);")
    assert _runner(mysql_db, tmp_path).run() == 0
    # Second run: nothing to do, still success.
    assert _runner(mysql_db, tmp_path).run() == 0


# --- failure + blocking -----------------------------------------------------

def test_failed_migration_records_and_blocks(mysql_db, tmp_path):
    _write(tmp_path, "v1.0_a.sql", "CREATE TABLE a (id INT PRIMARY KEY);")
    _write(tmp_path, "v1.1_bad.sql", "THIS IS NOT SQL;")
    _write(tmp_path, "v1.2_c.sql", "CREATE TABLE c (id INT PRIMARY KEY);")

    rc = _runner(mysql_db, tmp_path).run()
    assert rc == 1

    recs = _records(mysql_db)
    # v1.0 succeeded, v1.1 failed, v1.2 never ran.
    assert [(str(r.version), r.status) for r in recs] == [
        ("v1.0", STATUS_SUCCESS),
        ("v1.1", STATUS_FAILED),
    ]


def test_blocking_migration_non_interactive_stops(mysql_db, tmp_path):
    _write(tmp_path, "v1.0_bad.sql", "NOT SQL;")
    assert _runner(mysql_db, tmp_path).run() == 1

    # A second non-interactive run must refuse to retry and raise an error
    # whose message names the actual blocking status (not "previously ...").
    with pytest.raises(MysqlmigError) as exc:
        _runner(mysql_db, tmp_path).run()
    assert "is in state FAILED" in str(exc.value)


def test_init_creates_metadata(mysql_db, tmp_path):
    # init on an existing (empty) database creates the table and view.
    runner = _runner(mysql_db, tmp_path)
    assert runner.init() == 0
    assert runner.metadata.exists() is True


def test_init_idempotent_reports_already_initialized(mysql_db, tmp_path, capsys):
    runner = _runner(mysql_db, tmp_path)
    runner.init()
    capsys.readouterr()  # clear
    # Second init succeeds (exit 0) and reports it was already initialized.
    assert runner.init() == 0
    out = capsys.readouterr().out
    assert "lready initialized" in out


def test_init_does_not_create_database(mysql_db, tmp_path):
    # init must NOT create a missing database; it errors instead (a dedicated
    # migration user typically lacks CREATE DATABASE privilege).
    missing_db = mysql_db + "_missing"
    cfg = _config(missing_db, tmp_path)
    runner = Runner(cfg, interactive=False)
    with pytest.raises(MysqlmigError):
        runner.init()
    # The database was not created as a side effect.
    client = MysqlClient(ConnectionConfig(database=mysql_db))
    rows = client.query(
        "SELECT COUNT(*) FROM information_schema.schemata "
        f"WHERE schema_name = '{missing_db}';"
    )
    assert rows[0][0] == "0"


def test_failed_then_successful_retry(mysql_db, tmp_path):
    bad = tmp_path / "v1.0_fix.sql"
    _write(tmp_path, "v1.0_fix.sql", "NOT SQL;")
    assert _runner(mysql_db, tmp_path).run() == 1

    # Fix the migration file and retry interactively (auto-confirm).
    bad.write_text("CREATE TABLE fixed (id INT PRIMARY KEY);", encoding="utf-8")
    runner = _runner(mysql_db, tmp_path, interactive=True)
    runner._confirm = lambda prompt: True  # auto-confirm the retry
    assert runner.run() == 0

    recs = _records(mysql_db)
    assert len(recs) == 1
    assert recs[0].status == STATUS_SUCCESS
    # Log retains both attempts.
    log = _log(mysql_db, "v1.0")
    assert "ERROR" in log  # from the failed attempt


# --- consistency ------------------------------------------------------------

def test_status_uninitialized_errors(mysql_db, tmp_path):
    _write(tmp_path, "v1.0_a.sql", "CREATE TABLE a (id INT PRIMARY KEY);")
    with pytest.raises(MysqlmigError):
        _runner(mysql_db, tmp_path).status()


def test_success_record_missing_from_repo_errors(mysql_db, tmp_path):
    # Apply v1.0 and v1.1, then remove v1.1 from the repo and load state.
    _write(tmp_path, "v1.0_a.sql", "CREATE TABLE a (id INT PRIMARY KEY);")
    _write(tmp_path, "v1.1_b.sql", "CREATE TABLE b (id INT PRIMARY KEY);")
    assert _runner(mysql_db, tmp_path).run() == 0

    (tmp_path / "v1.1_b.sql").unlink()
    with pytest.raises(MigrationStateError):
        _runner(mysql_db, tmp_path).status()


def test_status_with_target_limits_plan(mysql_db, tmp_path, capsys):
    # Repo v1.0..v1.3, apply only v1.0, then preview `status --to v1.2`.
    for v in ("v1.0", "v1.1", "v1.2", "v1.3"):
        _write(tmp_path, f"{v}_t.sql",
               f"CREATE TABLE t_{v.replace('.', '_')} (id INT PRIMARY KEY);")
    assert _runner(mysql_db, tmp_path, target="v1.0").run() == 0
    capsys.readouterr()  # clear run output

    assert _runner(mysql_db, tmp_path, target="v1.2").status() == 0
    out = capsys.readouterr().out
    # Target reflects the requested version, not latest (v1.3). Labels are
    # right-aligned to the width of "Database".
    assert "Target: v1.2" in out
    assert "Latest: v1.3" in out
    # Pending is bounded by the target: v1.1 and v1.2 only, never v1.3.
    assert "v1.1_t.sql" in out
    assert "v1.2_t.sql" in out
    assert "v1.3_t.sql" not in out


def test_status_default_target_is_latest(mysql_db, tmp_path, capsys):
    # With no target, status previews up to the latest available version.
    for v in ("v1.0", "v1.1", "v1.2"):
        _write(tmp_path, f"{v}_t.sql",
               f"CREATE TABLE t_{v.replace('.', '_')} (id INT PRIMARY KEY);")
    assert _runner(mysql_db, tmp_path, target="v1.0").run() == 0
    capsys.readouterr()

    assert _runner(mysql_db, tmp_path).status() == 0
    out = capsys.readouterr().out
    assert "Target: v1.2" in out
    assert "v1.1_t.sql" in out
    assert "v1.2_t.sql" in out
