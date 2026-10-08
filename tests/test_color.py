"""Unit tests for the Color helper."""

import mysqlmig
from mysqlmig import (
    Color,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_SUCCESS,
)


RESET = "\033[0m"


# -- disabled = plain passthrough --------------------------------------------

def test_disabled_is_noop():
    c = Color(enabled=False)
    assert c.success("x") == "x"
    assert c.failure("x") == "x"
    assert c.warning("x") == "x"
    assert c.version("x") == "x"
    assert c.pending("x") == "x"
    assert c.heading("x") == "x"


def test_disabled_status_passthrough():
    c = Color(enabled=False)
    assert c.status(STATUS_SUCCESS) == STATUS_SUCCESS
    assert c.status(STATUS_FAILED) == STATUS_FAILED
    assert c.status(STATUS_IN_PROGRESS) == STATUS_IN_PROGRESS


# -- enabled = wrapped with ANSI codes ---------------------------------------

def test_enabled_wraps_and_resets():
    c = Color(enabled=True)
    out = c.success("ok")
    assert out.startswith("\033[32m")   # green
    assert out.endswith(RESET)
    assert "ok" in out


def test_enabled_colors_differ_by_semantics():
    c = Color(enabled=True)
    assert c.success("x") != c.failure("x")
    assert c.failure("x") != c.warning("x")


def test_status_colors_by_value():
    c = Color(enabled=True)
    assert c.status(STATUS_SUCCESS).startswith("\033[32m")   # green
    assert c.status(STATUS_FAILED).startswith("\033[31m")    # red
    assert c.status(STATUS_IN_PROGRESS).startswith("\033[33m")  # yellow


def test_status_unknown_value_passthrough():
    c = Color(enabled=True)
    assert c.status("WEIRD") == "WEIRD"


def test_status_colors_custom_text_for_alignment():
    # A pre-padded cell keeps its width; the padding is inside the color codes.
    c = Color(enabled=True)
    out = c.status(STATUS_SUCCESS, "SUCCESS     ")
    assert "SUCCESS     " in out
    assert out.startswith("\033[32m")
    assert out.endswith(RESET)


def test_non_string_values_coerced():
    c = Color(enabled=True)
    # version() is often passed a Version object.
    out = c.version(mysqlmig.Version("v1.2"))
    assert "v1.2" in out


# -- auto-enable logic -------------------------------------------------------

def test_no_color_env_disables(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert Color._auto_enabled() is False


def test_non_tty_disables(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)

    class FakeStdout:
        def isatty(self):
            return False

    monkeypatch.setattr("sys.stdout", FakeStdout())
    assert Color._auto_enabled() is False


def test_tty_without_no_color_enables(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)

    class FakeStdout:
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdout", FakeStdout())
    assert Color._auto_enabled() is True
