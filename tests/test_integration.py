"""Integration tests against a real MySQL server (throwaway database).

Marked `integration`; skipped automatically when MySQL is unreachable (see
conftest.mysql_db). They drive the real Runner/Metadata through the mysql client.
"""

import re
import time

import pytest

from mysqlmig import (
    Config,
    ConnectionConfig,
    Metadata,
    MigrationStateError,
    MysqlClient,
    MysqlmigError,
    Runner,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_SUCCESS,
    Version,
)

pytestmark = pytest.mark.integration


# --- helpers ----------------------------------------------------------------

def _write(dirpath, name, sql):
    (dirpath / name).write_text(sql, encoding="utf-8")


def _config(database, directory, target=None, dry_run=False, verbose=False):
    return Config(
        database=database,
        directory=str(directory),
        connection=ConnectionConfig(database=database),
        target=target,
        dry_run=dry_run,
        verbose=verbose,
    )


def _runner(database, directory, interactive=False, **kw):
    return Runner(_config(database, directory, **kw), interactive=interactive)


def _records(database):
    client = MysqlClient(ConnectionConfig(database=database))
    return Metadata(client).load_records()


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


# --- lifecycle --------------------------------------------------------------

def test_insert_then_finalize_sets_time(mysql_db):
    client = MysqlClient(ConnectionConfig(database=mysql_db))
    meta = Metadata(client)
    meta.create()
    meta.insert_in_progress("v1.0", "v1.0_initial.sql")

    recs = meta.load_records()
    assert len(recs) == 1
    assert recs[0].status == STATUS_IN_PROGRESS
    assert recs[0].execution_time is None

    time.sleep(1.1)
    meta.finalize("v1.0", STATUS_SUCCESS, "ok\n")
    recs = meta.load_records()
    assert recs[0].status == STATUS_SUCCESS
    # Duration format HH:MM:SS.mmm (3 decimals), and at least ~1 second elapsed.
    assert recs[0].execution_time is not None
    assert re.match(r"^\d{2}:\d{2}:\d{2}\.\d{3}$", recs[0].execution_time)
    assert recs[0].execution_time.startswith("00:00:01.")


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
    log = meta.get_log("v1.0")
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


def test_run_dry_run_changes_nothing(mysql_db, tmp_path):
    _write(tmp_path, "v1.0_a.sql", "CREATE TABLE a (id INT PRIMARY KEY);")
    rc = _runner(mysql_db, tmp_path, dry_run=True).run()
    assert rc == 0
    # Metadata was created but no migration recorded.
    recs = _records(mysql_db)
    assert recs == []


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
    # (surfaced as exit 1 via main-style handling); Runner.run raises here.
    with pytest.raises(MysqlmigError):
        _runner(mysql_db, tmp_path).run()


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
    client = MysqlClient(ConnectionConfig(database=mysql_db))
    log = Metadata(client).get_log("v1.0")
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
