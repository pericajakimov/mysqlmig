"""Unit tests for display-host resolution (configured host, never @@hostname)."""

import mysqlmig
from mysqlmig import ConnectionConfig, resolve_display_host


def test_cli_host_wins():
    conn = ConnectionConfig(host="aurora.example.com", database="app")
    assert resolve_display_host(conn) == "aurora.example.com"


def test_option_file_host_used_when_no_cli_host(monkeypatch):
    monkeypatch.setattr(mysqlmig, "_host_from_print_defaults",
                        lambda args, **kw: "db-from-cnf")
    conn = ConnectionConfig(database="app")  # no --host
    assert resolve_display_host(conn) == "db-from-cnf"


def test_localhost_when_nothing_configured(monkeypatch):
    monkeypatch.setattr(mysqlmig, "_host_from_print_defaults",
                        lambda args, **kw: None)
    conn = ConnectionConfig(database="app")
    assert resolve_display_host(conn) == "localhost"


def test_cli_host_takes_precedence_over_option_file(monkeypatch):
    # Even if print-defaults would return a host, the explicit CLI host wins
    # and print-defaults is not consulted.
    called = {"n": 0}

    def fake(args, **kw):
        called["n"] += 1
        return "should-not-be-used"

    monkeypatch.setattr(mysqlmig, "_host_from_print_defaults", fake)
    conn = ConnectionConfig(host="explicit-host", database="app")
    assert resolve_display_host(conn) == "explicit-host"
    assert called["n"] == 0


def test_print_defaults_parsing_extracts_host(monkeypatch):
    # Simulate the mysql --print-defaults output line and confirm parsing.
    class Proc:
        returncode = 0
        stdout = ("mysql would have been started with the following "
                  "arguments:\n--user=u --host=db.internal --port=3306\n")

    monkeypatch.setattr(mysqlmig.shutil, "which", lambda name: "/usr/bin/mysql")
    monkeypatch.setattr(mysqlmig.subprocess, "run", lambda *a, **k: Proc())
    assert mysqlmig._host_from_print_defaults([]) == "db.internal"


def test_print_defaults_no_host_returns_none(monkeypatch):
    class Proc:
        returncode = 0
        stdout = "mysql ...\n--user=u --port=3306\n"

    monkeypatch.setattr(mysqlmig.shutil, "which", lambda name: "/usr/bin/mysql")
    monkeypatch.setattr(mysqlmig.subprocess, "run", lambda *a, **k: Proc())
    assert mysqlmig._host_from_print_defaults([]) is None


def test_print_defaults_last_host_wins(monkeypatch):
    class Proc:
        returncode = 0
        stdout = "x\n--host=first --host=second\n"

    monkeypatch.setattr(mysqlmig.shutil, "which", lambda name: "/usr/bin/mysql")
    monkeypatch.setattr(mysqlmig.subprocess, "run", lambda *a, **k: Proc())
    assert mysqlmig._host_from_print_defaults([]) == "second"
