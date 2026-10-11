"""Unit tests for CLI parsing: option/command ordering and help rendering."""

import pytest

from mysqlmig import build_parser, render_help


def _parse(argv):
    return build_parser().parse_args(argv)


def _get(ns, name):
    return getattr(ns, name, None)


# -- option ordering ---------------------------------------------------------

def test_global_option_before_command():
    ns = _parse(["-D", "demo", "run"])
    assert ns.command == "run"
    assert _get(ns, "db") == "demo"


def test_global_option_after_command():
    ns = _parse(["run", "-D", "demo"])
    assert ns.command == "run"
    assert _get(ns, "db") == "demo"


def test_global_option_after_status():
    ns = _parse(["status", "-D", "demo"])
    assert ns.command == "status"
    assert _get(ns, "db") == "demo"


def test_run_only_and_global_mixed_after_command():
    ns = _parse(["run", "--to", "v1.1", "-D", "demo", "--verbose"])
    assert ns.command == "run"
    assert _get(ns, "db") == "demo"
    assert _get(ns, "to") == "v1.1"
    assert _get(ns, "verbose") is True


def test_h_is_host_not_help():
    ns = _parse(["status", "-h", "db-host"])
    assert _get(ns, "host") == "db-host"


def test_login_path_has_no_short_form():
    # --login-path works in full form.
    ns = _parse(["status", "--login-path", "prod"])
    assert _get(ns, "login_path") == "prod"
    # -L is NOT accepted (matches the mysql client, where -L is
    # --skip-line-numbers), so argparse rejects it.
    with pytest.raises(SystemExit):
        _parse(["status", "-L", "prod"])


def test_to_has_no_short_form():
    # --to works in full form.
    ns = _parse(["run", "--to", "v1.2"])
    assert _get(ns, "to") == "v1.2"
    # -t is NOT accepted (matches the mysql client, where -t is --table).
    with pytest.raises(SystemExit):
        _parse(["run", "-t", "v1.2"])


def test_defaults_group_suffix_parsed():
    ns = _parse(["status", "--defaults-group-suffix", "_db1"])
    assert _get(ns, "defaults_group_suffix") == "_db1"
    # Also accepts the = form, before the command.
    ns2 = _parse(["--defaults-group-suffix=_db2", "run"])
    assert _get(ns2, "defaults_group_suffix") == "_db2"


# -- password option (-p / --password), pre-extracted from argv ------------

def test_bare_p_is_prompt_sentinel():
    import mysqlmig
    rem, pw = mysqlmig._extract_password_arg(["status", "-p"])
    assert rem == ["status"]
    assert pw is mysqlmig._PROMPT_PASSWORD


def test_long_password_bare_is_prompt():
    import mysqlmig
    rem, pw = mysqlmig._extract_password_arg(["status", "--password"])
    assert pw is mysqlmig._PROMPT_PASSWORD


def test_password_equals_value():
    import mysqlmig
    rem, pw = mysqlmig._extract_password_arg(["run", "--password=secret", "-D", "demo"])
    assert pw == "secret"
    assert rem == ["run", "-D", "demo"]


def test_password_attached_short_form():
    import mysqlmig
    _, pw = mysqlmig._extract_password_arg(["status", "-psecret"])
    assert pw == "secret"


def test_p_does_not_consume_following_command():
    # `-p run` must prompt and keep `run` as the command (not eat it).
    import mysqlmig
    rem, pw = mysqlmig._extract_password_arg(["-p", "run"])
    assert rem == ["run"]
    assert pw is mysqlmig._PROMPT_PASSWORD


def test_no_password_option():
    import mysqlmig
    rem, pw = mysqlmig._extract_password_arg(["status"])
    assert pw is None
    assert rem == ["status"]


def test_no_command():
    ns = _parse([])
    assert ns.command is None


def test_init_command_parses():
    ns = _parse(["init", "-D", "demo"])
    assert ns.command == "init"
    assert _get(ns, "db") == "demo"


def test_dry_run_option_removed():
    # --dry-run no longer exists; argparse should reject it.
    import pytest
    with pytest.raises(SystemExit):
        _parse(["run", "--dry-run"])


def test_long_and_short_db_equivalent():
    assert _get(_parse(["run", "--db", "x"]), "db") == "x"
    assert _get(_parse(["run", "-D", "x"]), "db") == "x"


# -- help rendering ----------------------------------------------------------

# -- --to applies to status and run; --verbose to run only ------------------

def test_to_with_status_after_command():
    ns = _parse(["status", "--to", "v1.2"])
    assert ns.command == "status"
    assert _get(ns, "to") == "v1.2"


def test_to_with_status_before_command():
    ns = _parse(["--to", "v1.2", "status"])
    assert ns.command == "status"
    assert _get(ns, "to") == "v1.2"


def test_to_without_command_errors(capsys):
    from mysqlmig import main
    rc = main(["--to", "v1.0"])
    assert rc == 1
    err = capsys.readouterr().err
    # Now valid for both status and run, so the message names both.
    assert "--to" in err and "run" in err and "status" in err


def test_to_with_init_errors():
    # init does not define --to on its subparser, so argparse rejects it
    # (argparse exits via SystemExit for unrecognized arguments).
    from mysqlmig import main
    with pytest.raises(SystemExit):
        main(["init", "--to", "v1.0"])


def test_to_with_history_errors():
    from mysqlmig import main
    with pytest.raises(SystemExit):
        main(["history", "--to", "v1.0"])


def test_verbose_with_status_errors():
    # --verbose is run-only; status's subparser does not define it.
    from mysqlmig import main
    with pytest.raises(SystemExit):
        main(["status", "--verbose"])


def test_verbose_without_command_allowed_for_interactive():
    # --verbose with no command is valid: it enables verbose output for an
    # interactive run. The parser accepts it as a top-level flag.
    ns = _parse(["--verbose"])
    assert ns.command is None
    assert _get(ns, "verbose") is True


# -- help rendering ----------------------------------------------------------

def test_render_help_contains_sections():
    text = render_help()
    assert "Commands:" in text
    assert "Status/run options:" in text
    assert "Global options:" in text
    # Mentions that options can go before or after the command.
    assert "before or after" in text


def test_render_help_plain_when_color_disabled(monkeypatch):
    import mysqlmig
    monkeypatch.setattr(mysqlmig, "COLOR", mysqlmig.Color(enabled=False))
    text = render_help()
    assert "\033[" not in text  # no ANSI escape codes


def test_render_help_colors_when_enabled(monkeypatch):
    import mysqlmig
    monkeypatch.setattr(mysqlmig, "COLOR", mysqlmig.Color(enabled=True))
    text = render_help()
    assert "\033[" in text              # some coloring present
    assert "\033[1mCommands:\033[0m" in text          # heading bold
    assert "\033[32mstatus\033[0m" in text             # command green
    # A flag is colored and the VERSION|latest metavar stays one unit.
    assert "\033[36m--to\033[0m" in text
    assert "\033[33mVERSION|latest\033[0m" in text
