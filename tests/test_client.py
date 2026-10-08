"""Unit tests for mysql client command construction and output parsing.

These do not touch a real database; they verify how the invocation is built.
"""

from mysqlmig import ConnectionConfig, MysqlClient, _parse_batch_rows


def _client(**kwargs):
    return MysqlClient(ConnectionConfig(**kwargs))


# -- option ordering ---------------------------------------------------------

def test_option_file_args_come_first():
    client = _client(
        defaults_file="/etc/mysqlmig.cnf",
        host="db",
        user="u",
        database="app",
    )
    cmd = client._migration_command()
    # mysql binary is first, then the defaults-file option must precede
    # connection options (client requirement).
    assert cmd[0] == "mysql"
    assert cmd[1] == "--defaults-file=/etc/mysqlmig.cnf"
    assert cmd.index("--defaults-file=/etc/mysqlmig.cnf") < cmd.index("--host=db")


def test_defaults_extra_file_after_defaults_file():
    client = _client(
        defaults_file="/a.cnf",
        defaults_extra_file="/b.cnf",
    )
    cmd = client._migration_command()
    assert cmd[1] == "--defaults-file=/a.cnf"
    assert cmd[2] == "--defaults-extra-file=/b.cnf"


# -- fixed options -----------------------------------------------------------

def test_migration_command_has_fixed_options_and_no_force():
    cmd = _client(database="app")._migration_command()
    assert "-vvv" in cmd
    assert "--comments" in cmd
    assert "--force" not in cmd
    assert "-f" not in cmd


def test_migration_includes_database():
    cmd = _client(database="application")._migration_command()
    assert "--database=application" in cmd


# -- connection options ------------------------------------------------------

def test_connection_args_all_present():
    client = _client(
        host="db-prod",
        port=3306,
        user="migrator",
        login_path="production",
        database="app",
    )
    cmd = client._migration_command()
    assert "--login-path=production" in cmd
    assert "--host=db-prod" in cmd
    assert "--port=3306" in cmd
    assert "--user=migrator" in cmd
    assert "--database=app" in cmd


def test_empty_config_only_binary_and_fixed():
    # No connection info -> rely on the client's own option files.
    cmd = _client()._migration_command()
    assert cmd == ["mysql", "-vvv", "--comments"]


def test_metadata_command_batch_mode():
    cmd = _client(database="app")._metadata_command(["--batch", "--skip-column-names"])
    assert "--batch" in cmd
    assert "--skip-column-names" in cmd
    assert "-vvv" not in cmd  # fixed migration options are not used for metadata


# -- batch output parsing ----------------------------------------------------

def test_parse_batch_rows_basic():
    output = "1\tv1.0\tSUCCESS\n2\tv1.1\tFAILED\n"
    assert _parse_batch_rows(output) == [
        ["1", "v1.0", "SUCCESS"],
        ["2", "v1.1", "FAILED"],
    ]


def test_parse_batch_rows_empty():
    assert _parse_batch_rows("") == []
    assert _parse_batch_rows("\n") == []


def test_parse_batch_rows_single_column():
    assert _parse_batch_rows("v1.7\n") == [["v1.7"]]


def test_parse_batch_rows_preserves_null_literal():
    output = "1\tv1.0\tIN_PROGRESS\tNULL\n"
    assert _parse_batch_rows(output) == [["1", "v1.0", "IN_PROGRESS", "NULL"]]
