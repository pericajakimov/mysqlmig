"""Unit tests for configuration resolution (CLI + [mysqlmig] + defaults)."""

import os

import pytest

from mysqlmig import MysqlmigError, _resolve_config


class Args:
    """Minimal stand-in for parsed argparse args."""

    def __init__(self, **kw):
        defaults = {
            "db": None, "dir": None, "host": None, "port": None, "user": None,
            "login_path": None, "defaults_file": None, "defaults_extra_file": None,
            "to": None, "verbose": False,
        }
        defaults.update(kw)
        for key, value in defaults.items():
            setattr(self, key, value)


def group(**kw):
    """Return a group_reader that yields a fixed [mysqlmig] dict."""
    return lambda option_file_args: dict(kw)


def _resolve(args, group_reader, password=None):
    """_resolve_config with an injected no-op password reader (hermetic).

    `password`, if given, is what the injected reader returns for the
    [mysqlmig] password.
    """
    return _resolve_config(
        args,
        group_reader=group_reader,
        password_reader=lambda **kw: password,
    )


# -- database resolution -----------------------------------------------------

def test_db_from_cli():
    cfg = _resolve(Args(db="appcli"), group())
    assert cfg.database == "appcli"


def test_db_from_group():
    cfg = _resolve(Args(), group(db="appcfg"))
    assert cfg.database == "appcfg"


def test_cli_db_overrides_group():
    cfg = _resolve(Args(db="appcli"), group(db="appcfg"))
    assert cfg.database == "appcli"


def test_missing_db_errors():
    with pytest.raises(MysqlmigError):
        _resolve(Args(), group())


# -- directory resolution ----------------------------------------------------

def test_cli_db_maps_to_migrations_subfolder():
    # --db=demo (CLI) with no --dir -> migrations/demo
    cfg = _resolve(Args(db="demo"), group())
    assert cfg.directory == os.path.join("migrations", "demo")


def test_config_db_maps_to_bare_migrations():
    # db comes from [mysqlmig], not CLI -> bare 'migrations' folder, no subfolder
    cfg = _resolve(Args(), group(db="demo"))
    assert cfg.directory == "migrations"


def test_explicit_dir_used_literally_over_cli_db():
    # --db=demo --dir=/opt/m/demo -> that folder exactly
    cfg = _resolve(Args(db="demo", dir="/opt/m/demo"), group())
    assert cfg.directory == "/opt/m/demo"


def test_explicit_dir_from_group_used_literally():
    # dir from [mysqlmig] is also a literal override, even for a config-default db
    cfg = _resolve(Args(), group(db="demo", dir="/opt/custom"))
    assert cfg.directory == "/opt/custom"


def test_cli_dir_overrides_group_dir():
    cfg = _resolve(Args(db="app", dir="cli_dir"), group(dir="cfg_dir"))
    assert cfg.directory == "cli_dir"


# -- login path --------------------------------------------------------------

def test_login_path_from_group():
    cfg = _resolve(Args(db="app"), group(**{"login-path": "mp"}))
    assert cfg.connection.login_path == "mp"


def test_cli_login_path_overrides_group():
    cfg = _resolve(Args(db="app", login_path="cli"),
                   group(**{"login-path": "cfg"}))
    assert cfg.connection.login_path == "cli"


# -- connection pass-through -------------------------------------------------

def test_connection_options_passed_through():
    cfg = _resolve(
        Args(db="app", host="h", port="3307", user="u",
             defaults_file="/f.cnf", defaults_extra_file="/e.cnf"),
        group(),
    )
    conn = cfg.connection
    assert conn.host == "h"
    assert conn.port == "3307"
    assert conn.user == "u"
    assert conn.database == "app"
    assert conn.defaults_file == "/f.cnf"
    assert conn.defaults_extra_file == "/e.cnf"


def test_run_options_carried_into_config():
    cfg = _resolve(Args(db="app", to="v1.5", verbose=True), group())
    assert cfg.target == "v1.5"
    assert cfg.verbose is True


# -- host / port / user from [mysqlmig] --------------------------------------

def test_host_port_user_from_group():
    cfg = _resolve(
        Args(db="app"),
        group(host="cfg-host", port="3307", user="migrator"),
    )
    assert cfg.connection.host == "cfg-host"
    assert cfg.connection.port == "3307"
    assert cfg.connection.user == "migrator"


def test_cli_host_port_user_override_group():
    cfg = _resolve(
        Args(db="app", host="cli-host", port="3399", user="cliuser"),
        group(host="cfg-host", port="3307", user="migrator"),
    )
    assert cfg.connection.host == "cli-host"
    assert cfg.connection.port == "3399"
    assert cfg.connection.user == "cliuser"


# -- password precedence -----------------------------------------------------

def test_password_from_group_file():
    # No -p prompt; password comes from the (injected) [mysqlmig] reader.
    cfg = _resolve(Args(db="app"), group(), password="cfg_pw")
    assert cfg.connection.password == "cfg_pw"


def test_prompt_password_overrides_group():
    cfg = _resolve_config(
        Args(db="app"),
        group_reader=group(),
        prompted_password="typed_pw",
        password_reader=lambda **kw: "cfg_pw",
    )
    assert cfg.connection.password == "typed_pw"


def test_empty_prompt_password_is_honored_not_fallback():
    # An empty prompted password (user pressed Enter) means an EMPTY password,
    # not a fallback to the [mysqlmig]/option-file password. Matches the mysql
    # client. The empty value must still be passed as --password= to the client.
    from mysqlmig import MysqlClient
    cfg = _resolve_config(
        Args(db="app"),
        group_reader=group(),
        prompted_password="",                 # blank prompt
        password_reader=lambda **kw: "cfg_pw",  # would be the fallback
    )
    assert cfg.connection.password == ""
    cmd = MysqlClient(cfg.connection)._migration_command()
    assert "--password=" in cmd


def test_no_password_when_neither():
    cfg = _resolve(Args(db="app"), group(), password=None)
    assert cfg.connection.password is None


# -- password on the command line (matches the mysql client) -----------------

def test_password_on_command_line_when_set():
    # An explicit password is passed as --password= so it overrides any
    # option-file password (which MYSQL_PWD would not). This is ps-visible,
    # the same tradeoff as `mysql --password=`.
    from mysqlmig import ConnectionConfig, MysqlClient
    conn = ConnectionConfig(host="h", user="u", database="app",
                            password="s3cret")
    cmd = MysqlClient(conn)._migration_command()
    assert "--password=s3cret" in cmd


def test_no_password_arg_when_unset():
    # Without a password, nothing is appended -> the client's own auth applies.
    from mysqlmig import ConnectionConfig, MysqlClient
    conn = ConnectionConfig(database="app")
    cmd = MysqlClient(conn)._migration_command()
    assert not any(a.startswith("--password") for a in cmd)


# -- reading the [mysqlmig] password from an option file ---------------------

def test_read_password_from_defaults_file(tmp_path):
    from mysqlmig import _read_mysqlmig_password
    cnf = tmp_path / "mysqlmig.cnf"
    cnf.write_text(
        "[mysqlmig]\ndb=demo\nuser=migrator\npassword=p@ss w0rd!\n",
        encoding="utf-8",
    )
    pw = _read_mysqlmig_password(defaults_file=str(cnf))
    assert pw == "p@ss w0rd!"


def test_read_password_absent_returns_none(tmp_path):
    from mysqlmig import _read_mysqlmig_password
    cnf = tmp_path / "mysqlmig.cnf"
    cnf.write_text("[mysqlmig]\ndb=demo\nuser=migrator\n", encoding="utf-8")
    assert _read_mysqlmig_password(defaults_file=str(cnf)) is None


def test_read_password_unreadable_file_is_safe(tmp_path):
    from mysqlmig import _read_mysqlmig_password
    missing = tmp_path / "nope.cnf"
    # Best-effort: a missing file must not raise.
    assert _read_mysqlmig_password(defaults_file=str(missing)) is None
