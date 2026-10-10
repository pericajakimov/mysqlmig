"""Unit tests for migration filename parsing and directory discovery."""

import pytest

from mysqlmig import (
    MigrationDiscoveryError,
    discover_migrations,
    parse_migration_filename,
)

# -- filename parsing --------------------------------------------------------

@pytest.mark.parametrize(
    "filename, version_raw, description",
    [
        ("v1.0_initial.sql", "v1.0", "initial"),
        ("v1.1_add_users.sql", "v1.1", "add_users"),
        ("v1.2.3_add_indexes.sql", "v1.2.3", "add_indexes"),
        ("v1.10.20_cleanup.sql", "v1.10.20", "cleanup"),
        ("1.0_initial.sql", "1.0", "initial"),
        ("v1.3_add-order-indexes.sql", "v1.3", "add-order-indexes"),
        ("v1.4_desc.with.dots.sql", "v1.4", "desc.with.dots"),
    ],
)
def test_parse_valid_filenames(filename, version_raw, description):
    migration = parse_migration_filename(filename)
    assert migration is not None
    assert str(migration.version) == version_raw
    assert migration.description == description
    assert migration.filename == filename


@pytest.mark.parametrize(
    "filename",
    [
        "README.md",
        "notes.txt",
        "v1.0_initial.txt",      # wrong extension
        "v1.0.sql",              # no description/underscore
        "_initial.sql",          # no version
        "initial.sql",           # no version
        "vx.0_initial.sql",      # non-numeric version
        "v1.0_.sql",             # empty description
        "v1.0_initial.SQL",      # case-sensitive extension
    ],
)
def test_parse_ignored_filenames(filename):
    assert parse_migration_filename(filename) is None


# -- discovery ---------------------------------------------------------------

def _write(dirpath, *names):
    for name in names:
        (dirpath / name).write_text("SELECT 1;\n", encoding="utf-8")


def test_missing_directory(tmp_path):
    with pytest.raises(MigrationDiscoveryError):
        discover_migrations(str(tmp_path / "does_not_exist"))


def test_unreadable_directory_raises_clean_error(tmp_path, monkeypatch):
    # Simulate a permission error from listing the directory; it should surface
    # as MigrationDiscoveryError, not a raw OSError/traceback.
    import mysqlmig

    def boom(path):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(mysqlmig.os, "listdir", boom)
    with pytest.raises(MigrationDiscoveryError):
        discover_migrations(str(tmp_path))


def test_discovery_ignores_non_migration_files(tmp_path):
    _write(tmp_path, "v1.0_initial.sql", "v1.1_add_users.sql",
           "README.md", "notes.txt")
    (tmp_path / "rollback").mkdir()
    migrations = discover_migrations(str(tmp_path))
    assert [m.filename for m in migrations] == [
        "v1.0_initial.sql",
        "v1.1_add_users.sql",
    ]


def test_discovery_sorts_numerically(tmp_path):
    _write(tmp_path, "v1.10_c.sql", "v1.2_b.sql", "v1.0_a.sql", "v1.9_d.sql")
    migrations = discover_migrations(str(tmp_path))
    assert [str(m.version) for m in migrations] == ["v1.0", "v1.2", "v1.9", "v1.10"]


def test_discovery_allows_gaps(tmp_path):
    _write(tmp_path, "v1.0_a.sql", "v1.2_b.sql", "v1.5_c.sql")
    migrations = discover_migrations(str(tmp_path))
    assert [str(m.version) for m in migrations] == ["v1.0", "v1.2", "v1.5"]


def test_duplicate_versions_rejected(tmp_path):
    # v1.10 and 1.10 resolve to the same numeric version.
    _write(tmp_path, "v1.10_add_index.sql", "1.10_cleanup.sql")
    with pytest.raises(MigrationDiscoveryError):
        discover_migrations(str(tmp_path))


def test_mixed_prefix_convention_rejected(tmp_path):
    _write(tmp_path, "v1.0_a.sql", "1.1_b.sql")
    with pytest.raises(MigrationDiscoveryError):
        discover_migrations(str(tmp_path))


def test_consistent_prefix_v(tmp_path):
    _write(tmp_path, "v1.0_a.sql", "v1.1_b.sql")
    migrations = discover_migrations(str(tmp_path))
    assert all(m.version.has_prefix for m in migrations)


def test_consistent_prefix_none(tmp_path):
    _write(tmp_path, "1.0_a.sql", "1.1_b.sql")
    migrations = discover_migrations(str(tmp_path))
    assert all(not m.version.has_prefix for m in migrations)


def test_empty_directory(tmp_path):
    migrations = discover_migrations(str(tmp_path))
    assert migrations == []
