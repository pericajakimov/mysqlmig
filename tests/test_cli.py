"""Unit tests for CLI parsing: option/command ordering and help rendering."""

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


def test_no_command():
    ns = _parse([])
    assert ns.command is None


def test_long_and_short_db_equivalent():
    assert _get(_parse(["run", "--db", "x"]), "db") == "x"
    assert _get(_parse(["run", "-D", "x"]), "db") == "x"


# -- help rendering ----------------------------------------------------------

def test_render_help_contains_sections():
    text = render_help()
    assert "Commands:" in text
    assert "Run options:" in text
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
