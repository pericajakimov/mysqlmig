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
            "to": None, "dry_run": False, "verbose": False,
        }
        defaults.update(kw)
        for key, value in defaults.items():
            setattr(self, key, value)


def group(**kw):
    """Return a group_reader that yields a fixed [mysqlmig] dict."""
    return lambda option_file_args: dict(kw)


# -- database resolution -----------------------------------------------------

def test_db_from_cli():
    cfg = _resolve_config(Args(db="appcli"), group_reader=group())
    assert cfg.database == "appcli"


def test_db_from_group():
    cfg = _resolve_config(Args(), group_reader=group(db="appcfg"))
    assert cfg.database == "appcfg"


def test_cli_db_overrides_group():
    cfg = _resolve_config(Args(db="appcli"), group_reader=group(db="appcfg"))
    assert cfg.database == "appcli"


def test_missing_db_errors():
    with pytest.raises(MysqlmigError):
        _resolve_config(Args(), group_reader=group())


# -- directory resolution ----------------------------------------------------

def test_cli_db_maps_to_migrations_subfolder():
    # --db=demo (CLI) with no --dir -> migrations/demo
    cfg = _resolve_config(Args(db="demo"), group_reader=group())
    assert cfg.directory == os.path.join("migrations", "demo")


def test_config_db_maps_to_bare_migrations():
    # db comes from [mysqlmig], not CLI -> bare 'migrations' folder, no subfolder
    cfg = _resolve_config(Args(), group_reader=group(db="demo"))
    assert cfg.directory == "migrations"


def test_explicit_dir_used_literally_over_cli_db():
    # --db=demo --dir=/opt/m/demo -> that folder exactly
    cfg = _resolve_config(
        Args(db="demo", dir="/opt/m/demo"),
        group_reader=group(),
    )
    assert cfg.directory == "/opt/m/demo"


def test_explicit_dir_from_group_used_literally():
    # dir from [mysqlmig] is also a literal override, even for a config-default db
    cfg = _resolve_config(Args(), group_reader=group(db="demo", dir="/opt/custom"))
    assert cfg.directory == "/opt/custom"


def test_cli_dir_overrides_group_dir():
    cfg = _resolve_config(
        Args(db="app", dir="cli_dir"),
        group_reader=group(dir="cfg_dir"),
    )
    assert cfg.directory == "cli_dir"


# -- login path --------------------------------------------------------------

def test_login_path_from_group():
    cfg = _resolve_config(Args(db="app"), group_reader=group(**{"login-path": "mp"}))
    assert cfg.connection.login_path == "mp"


def test_cli_login_path_overrides_group():
    cfg = _resolve_config(
        Args(db="app", login_path="cli"),
        group_reader=group(**{"login-path": "cfg"}),
    )
    assert cfg.connection.login_path == "cli"


# -- connection pass-through -------------------------------------------------

def test_connection_options_passed_through():
    cfg = _resolve_config(
        Args(db="app", host="h", port="3307", user="u",
             defaults_file="/f.cnf", defaults_extra_file="/e.cnf"),
        group_reader=group(),
    )
    conn = cfg.connection
    assert conn.host == "h"
    assert conn.port == "3307"
    assert conn.user == "u"
    assert conn.database == "app"
    assert conn.defaults_file == "/f.cnf"
    assert conn.defaults_extra_file == "/e.cnf"


def test_run_options_carried_into_config():
    cfg = _resolve_config(
        Args(db="app", to="v1.5", dry_run=True, verbose=True),
        group_reader=group(),
    )
    assert cfg.target == "v1.5"
    assert cfg.dry_run is True
    assert cfg.verbose is True
