"""Unit tests for the interactive menu loop (no database).

The menu's Runner is replaced with a stub that records which command was
invoked, so routing and loop/exit behavior can be tested without MySQL.
"""

from mysqlmig import HELP_TEXT, Config, ConnectionConfig, InteractiveMenu, MysqlmigError


class StubClient:
    """Stub mysql client: _display_host queries @@hostname; return nothing."""

    def query(self, sql):
        return []


class StubMetadata:
    def __init__(self, initialized=True):
        self._initialized = initialized

    def exists(self):
        return self._initialized


class StubState:
    def __init__(self, pending):
        self._pending = list(pending)

    def pending(self, target):
        return self._pending


class StubRunner:
    def __init__(self, config=None, initialized=True, pending=("m",)):
        self.calls = []
        self.client = StubClient()
        self.metadata = StubMetadata(initialized)
        self.config = config
        self.seen_target = None
        self._pending = list(pending)

    def _load_state(self):
        return StubState(self._pending)

    def status(self):
        self.calls.append("status")
        return 0

    def history(self):
        self.calls.append("history")
        return 0

    def run(self):
        self.calls.append("run")
        # Capture the target the menu set on the config for this run.
        if self.config is not None:
            self.seen_target = self.config.target
        return 0


def _menu(inputs):
    """Build an InteractiveMenu driven by a scripted list of input lines."""
    config = Config(
        database="app",
        directory="app",
        connection=ConnectionConfig(database="app"),
    )
    output = []
    it = iter(inputs)

    def fake_input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    menu = InteractiveMenu(config, input_fn=fake_input,
                           output_fn=lambda msg="": output.append(msg))
    stub = StubRunner(config)
    menu.runner = stub
    return menu, stub, output


def test_exit_choice_stops_loop():
    menu, stub, _ = _menu(["5"])
    assert menu.run() == 0
    assert stub.calls == []


def test_eof_exits_cleanly():
    menu, stub, _ = _menu([])
    assert menu.run() == 0
    assert stub.calls == []


def test_status_then_exit():
    menu, stub, _ = _menu(["1", "5"])
    menu.run()
    assert stub.calls == ["status"]


def test_history_then_exit():
    menu, stub, _ = _menu(["2", "5"])
    menu.run()
    assert stub.calls == ["history"]


def test_run_then_exit():
    # Run (3) prompts for a target (blank = latest), shows status first.
    menu, stub, _ = _menu(["3", "", "5"])
    menu.run()
    assert stub.calls == ["status", "run"]


def test_multiple_actions_before_exit():
    # Run also prompts for a target; supply blank after choosing 3.
    menu, stub, _ = _menu(["1", "2", "3", "", "5"])
    menu.run()
    assert stub.calls == ["status", "history", "status", "run"]


def test_help_prints_help_and_continues():
    menu, stub, output = _menu(["4", "5"])
    menu.run()
    assert any(HELP_TEXT in line for line in output)
    assert stub.calls == []


def test_invalid_choice_reprompts():
    menu, stub, output = _menu(["x", "1", "5"])
    menu.run()
    assert stub.calls == ["status"]
    assert any("Invalid choice" in line for line in output)


def test_quit_word_exits():
    menu, stub, _ = _menu(["quit"])
    assert menu.run() == 0
    assert stub.calls == []


def test_header_shows_version_and_database():
    menu, stub, output = _menu(["5"])
    menu.run()
    joined = "\n".join(output)
    assert "MySQL Migration Tool v" in joined
    assert "Database: app" in joined


def test_header_shows_configured_host(monkeypatch):
    import mysqlmig
    monkeypatch.setattr(mysqlmig, "resolve_display_host",
                        lambda conn: "aurora.example.com")
    menu, stub, output = _menu(["5"])
    menu.run()
    joined = "\n".join(output)
    assert "Host:     aurora.example.com" in joined


def test_header_shows_localhost_when_no_host_configured(monkeypatch):
    import mysqlmig
    monkeypatch.setattr(mysqlmig, "resolve_display_host",
                        lambda conn: "localhost")
    menu, stub, output = _menu(["5"])
    menu.run()
    joined = "\n".join(output)
    assert "Host:     localhost" in joined


def test_command_error_does_not_crash_loop():
    menu, stub, output = _menu(["1", "5"])

    def boom():
        raise MysqlmigError("boom")

    menu.runner.status = boom
    assert menu.run() == 0
    assert any("ERROR: boom" in line for line in output)


# -- run target prompt -------------------------------------------------------

def _menu_with(stub, inputs):
    """Build a menu wired to a specific stub, driven by scripted input."""
    output = []
    it = iter(inputs)

    def fake_input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    menu = InteractiveMenu(stub.config, input_fn=fake_input,
                           output_fn=lambda m="": output.append(m))
    menu.runner = stub
    return menu, output


def _config():
    return Config(database="app", directory="app",
                  connection=ConnectionConfig(database="app"))


def test_run_shows_status_before_prompting():
    # Initialized db with pending: status shown, then prompt, then run.
    cfg = _config()
    stub = StubRunner(cfg, initialized=True, pending=["m1"])
    menu, _ = _menu_with(stub, ["3", "v1.2", "5"])
    menu.run()
    assert stub.calls == ["status", "run"]
    assert stub.seen_target == "v1.2"
    assert menu.config.target is None  # restored after the run


def test_run_blank_target_means_latest():
    from mysqlmig import LATEST
    cfg = _config()
    stub = StubRunner(cfg, initialized=True, pending=["m1"])
    menu, _ = _menu_with(stub, ["3", "", "5"])
    menu.run()
    assert stub.seen_target == LATEST


def test_run_no_pending_does_not_prompt_or_run():
    # Initialized db already at latest: status shown, no prompt, no run.
    # (The real status() prints "No pending migrations"; the stub only records
    # the call, so we assert on behavior, not that message.)
    cfg = _config()
    stub = StubRunner(cfg, initialized=True, pending=[])
    menu, output = _menu_with(stub, ["3", "5"])
    menu.run()
    assert stub.calls == ["status"]  # run NOT called, no second prompt consumed
    assert stub.seen_target is None


def test_run_uninitialized_lists_pending_then_runs(monkeypatch):
    import mysqlmig

    class M:
        def __init__(self, filename):
            self.filename = filename

    monkeypatch.setattr(mysqlmig, "discover_migrations",
                        lambda d: [M("v1.0_a.sql"), M("v1.1_b.sql")])
    cfg = _config()
    stub = StubRunner(cfg, initialized=False)
    menu, output = _menu_with(stub, ["3", "", "5"])
    menu.run()
    assert stub.calls == ["run"]  # status not called (no table yet)
    joined = "\n".join(output)
    assert "does not exist" in joined
    assert "v1.0_a.sql" in joined        # pending list shown
    assert "v1.1_b.sql" in joined


def test_run_uninitialized_empty_repo_no_run(monkeypatch):
    import mysqlmig
    monkeypatch.setattr(mysqlmig, "discover_migrations", lambda d: [])
    cfg = _config()
    stub = StubRunner(cfg, initialized=False)
    menu, output = _menu_with(stub, ["3", "5"])
    menu.run()
    assert stub.calls == []  # nothing to run
    assert any("No migrations found" in line for line in output)


# -- separator ---------------------------------------------------------------

def test_separator_shown_between_output_and_menu():
    menu, stub, output = _menu(["1", "5"])
    menu.run()
    # A separator rule (a run of dashes) appears between action and menu.
    assert any(line and set(line) == {"-"} for line in output)
