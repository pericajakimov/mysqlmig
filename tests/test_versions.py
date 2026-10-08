"""Unit tests for version parsing and numeric comparison."""

import pytest

from mysqlmig import MigrationFormatError, Version


@pytest.mark.parametrize(
    "raw, components, has_prefix",
    [
        ("v1", (1,), True),
        ("1", (1,), False),
        ("v1.0", (1, 0), True),
        ("1.10", (1, 10), False),
        ("v1.2.3", (1, 2, 3), True),
        ("v1.10.20", (1, 10, 20), True),
        ("v1.2.3.4", (1, 2, 3, 4), True),
    ],
)
def test_parse_valid_versions(raw, components, has_prefix):
    v = Version(raw)
    assert v.components == components
    assert v.has_prefix is has_prefix
    # Stored exactly as written.
    assert str(v) == raw


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "v",
        "x1.0",
        "1.",
        ".1",
        "1..2",
        "v1.0-beta",
        "1,0",
        "v 1.0",
        "latest",
    ],
)
def test_parse_invalid_versions(raw):
    with pytest.raises(MigrationFormatError):
        Version(raw)


def test_numeric_ordering_not_lexical():
    assert Version("v1.9") < Version("v1.10")
    assert Version("1.9") < Version("1.10")
    assert Version("v1.2") < Version("v1.10")
    assert Version("v2") > Version("v1.99")


def test_equality_is_numeric_regardless_of_prefix():
    # Same numeric components => equal, even if written differently.
    assert Version("v1.10") == Version("1.10")
    assert Version("v1.0") == Version("1.0")
    assert hash(Version("v1.10")) == hash(Version("1.10"))


def test_different_components_not_equal():
    assert Version("v1.0") != Version("v1.0.0")  # (1,0) vs (1,0,0)
    assert Version("v1.1") != Version("v1.10")


def test_sorting():
    versions = [Version(v) for v in ["v1.10", "v1.2", "v1.0", "v1.9", "v2.0"]]
    ordered = sorted(versions)
    assert [str(v) for v in ordered] == ["v1.0", "v1.2", "v1.9", "v1.10", "v2.0"]
