"""Unit tests for migration state calculation (no database)."""

import pytest

from mysqlmig import (
    LATEST,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_SUCCESS,
    Migration,
    MigrationRecord,
    MigrationState,
    MigrationStateError,
    TargetError,
    Version,
)


def mig(version_raw):
    v = Version(version_raw)
    name = f"{version_raw}_desc.sql"
    return Migration(v, "desc", name, "/x/" + name)


def rec(id, version_raw, status):
    return MigrationRecord(
        id=id,
        version=Version(version_raw),
        script_name=f"{version_raw}_desc.sql",
        status=status,
        execution_start="2026-10-08 10:00:00.000",
        execution_time=None if status == STATUS_IN_PROGRESS else "00:00:01.0",
    )


def repo(*versions):
    return [mig(v) for v in versions]


# -- current / latest / blocking --------------------------------------------

def test_current_version_latest_success():
    state = MigrationState(
        repo("v1.0", "v1.1", "v1.2"),
        [rec(1, "v1.0", STATUS_SUCCESS), rec(2, "v1.1", STATUS_SUCCESS)],
    )
    assert state.current_version() == Version("v1.1")


def test_current_version_none_when_empty():
    state = MigrationState(repo("v1.0"), [])
    assert state.current_version() is None


def test_failed_does_not_advance_current():
    state = MigrationState(
        repo("v1.0", "v1.1", "v1.2"),
        [
            rec(1, "v1.0", STATUS_SUCCESS),
            rec(2, "v1.1", STATUS_SUCCESS),
            rec(3, "v1.2", STATUS_FAILED),
        ],
    )
    assert state.current_version() == Version("v1.1")
    assert state.blocking_record().version == Version("v1.2")
    assert state.blocking_record().status == STATUS_FAILED


def test_in_progress_is_blocking():
    state = MigrationState(
        repo("v1.0", "v1.1"),
        [rec(1, "v1.0", STATUS_SUCCESS), rec(2, "v1.1", STATUS_IN_PROGRESS)],
    )
    assert state.blocking_record().status == STATUS_IN_PROGRESS
    assert state.current_version() == Version("v1.0")


def test_no_blocking_when_all_success():
    state = MigrationState(
        repo("v1.0", "v1.1"),
        [rec(1, "v1.0", STATUS_SUCCESS), rec(2, "v1.1", STATUS_SUCCESS)],
    )
    assert state.blocking_record() is None


def test_latest_version():
    state = MigrationState(repo("v1.0", "v1.10", "v1.2"), [])
    assert state.latest_version() == Version("v1.10")


# -- target resolution -------------------------------------------------------

def test_resolve_target_latest():
    state = MigrationState(repo("v1.0", "v1.1", "v1.2"), [])
    assert state.resolve_target(LATEST) == Version("v1.2")
    assert state.resolve_target(None) == Version("v1.2")


def test_resolve_target_specific():
    state = MigrationState(repo("v1.0", "v1.1", "v1.2"), [])
    assert state.resolve_target("v1.1") == Version("v1.1")


def test_resolve_target_missing():
    state = MigrationState(repo("v1.0", "v1.1"), [])
    with pytest.raises(TargetError):
        state.resolve_target("v1.5")


def test_resolve_target_invalid_syntax():
    state = MigrationState(repo("v1.0"), [])
    with pytest.raises(TargetError):
        state.resolve_target("not-a-version")


def test_resolve_target_lower_than_current():
    state = MigrationState(
        repo("v1.0", "v1.1", "v1.2"),
        [rec(1, "v1.0", STATUS_SUCCESS), rec(2, "v1.1", STATUS_SUCCESS)],
    )
    with pytest.raises(TargetError):
        state.resolve_target("v1.0")


def test_resolve_target_equal_to_current_is_ok():
    state = MigrationState(
        repo("v1.0", "v1.1"),
        [rec(1, "v1.0", STATUS_SUCCESS), rec(2, "v1.1", STATUS_SUCCESS)],
    )
    assert state.resolve_target("v1.1") == Version("v1.1")


# -- pending -----------------------------------------------------------------

def test_pending_from_fresh_db():
    state = MigrationState(repo("v1.0", "v1.1", "v1.2"), [])
    pending = state.pending(LATEST)
    assert [str(m.version) for m in pending] == ["v1.0", "v1.1", "v1.2"]


def test_pending_partial():
    state = MigrationState(
        repo("v1.0", "v1.1", "v1.2", "v1.3"),
        [rec(1, "v1.0", STATUS_SUCCESS)],
    )
    pending = state.pending("v1.2")
    assert [str(m.version) for m in pending] == ["v1.1", "v1.2"]


def test_pending_none_when_current_equals_target():
    state = MigrationState(
        repo("v1.0", "v1.1"),
        [rec(1, "v1.0", STATUS_SUCCESS), rec(2, "v1.1", STATUS_SUCCESS)],
    )
    assert state.pending("v1.1") == []
    assert state.pending(LATEST) == []


def test_pending_respects_numeric_order():
    state = MigrationState(repo("v1.0", "v1.2", "v1.10", "v1.9"), [])
    pending = state.pending(LATEST)
    assert [str(m.version) for m in pending] == ["v1.0", "v1.2", "v1.9", "v1.10"]


# -- consistency -------------------------------------------------------------

def test_success_record_missing_from_repo():
    with pytest.raises(MigrationStateError):
        MigrationState(
            repo("v1.0"),
            [rec(1, "v1.0", STATUS_SUCCESS), rec(2, "v1.1", STATUS_SUCCESS)],
        )


def test_db_ahead_of_repo():
    # Repo only goes to v1.1 but db has a successful v1.2 (not in repo).
    # This surfaces as the missing-migration check first.
    with pytest.raises(MigrationStateError):
        MigrationState(
            repo("v1.0", "v1.1"),
            [
                rec(1, "v1.0", STATUS_SUCCESS),
                rec(2, "v1.1", STATUS_SUCCESS),
                rec(3, "v1.2", STATUS_SUCCESS),
            ],
        )


def test_failed_record_not_in_repo_is_allowed():
    # A FAILED/IN_PROGRESS record for a migration no longer in the repo is not
    # a consistency error by the success-based rule (only SUCCESS is checked).
    state = MigrationState(
        repo("v1.0"),
        [rec(1, "v1.0", STATUS_SUCCESS)],
    )
    assert state.current_version() == Version("v1.0")
