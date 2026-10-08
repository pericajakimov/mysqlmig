"""Unit tests for SQL string-literal quoting."""

from mysqlmig import _sql_quote


def test_plain_string():
    assert _sql_quote("v1.0") == "'v1.0'"


def test_none_is_null():
    assert _sql_quote(None) == "NULL"


def test_single_quote_escaped():
    assert _sql_quote("O'Brien") == "'O\\'Brien'"


def test_backslash_escaped_before_quote():
    # A trailing backslash must not escape the closing quote.
    assert _sql_quote("path\\") == "'path\\\\'"


def test_backslash_and_quote_combo():
    assert _sql_quote("a\\'b") == "'a\\\\\\'b'"


def test_nul_byte_stripped():
    assert _sql_quote("a\x00b") == "'ab'"


def test_newlines_preserved():
    # Newlines in captured log output are fine inside a quoted literal.
    assert _sql_quote("line1\nline2") == "'line1\nline2'"
