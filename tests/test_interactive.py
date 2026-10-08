"""Unit tests for the interactive menu loop (no database).

The menu's Runner is replaced with a stub that records which command was
invoked, so routing and loop/exit behavior can be tested without MySQL.
"""

from mysqlmig import Config, ConnectionConfig, HELP_TEXT, InteractiveMenu, MysqlmigError


class StubRunner:
    def __init__(self):
        self.calls = []

    def status(self):
        self.calls.append("status")
        return 0

    def history(self):
        self.calls.append("history")
        return 0

    def run(self):
        self.calls.append("run")
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
            raise EOFError

    menu = InteractiveMenu(config, input_fn=fake_input,
                           output_fn=lambda msg="": output.append(msg))
    stub = StubRunner()
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
    menu, stub, _ = _menu(["3", "5"])
    menu.run()
    assert stub.calls == ["run"]


def test_multiple_actions_before_exit():
    menu, stub, _ = _menu(["1", "2", "3", "5"])
    menu.run()
    assert stub.calls == ["status", "history", "run"]


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


def test_command_error_does_not_crash_loop():
    menu, stub, output = _menu(["1", "5"])

    def boom():
        raise MysqlmigError("boom")

    menu.runner.status = boom
    assert menu.run() == 0
    assert any("ERROR: boom" in line for line in output)
