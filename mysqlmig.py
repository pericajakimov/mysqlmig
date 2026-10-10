#!/usr/bin/env python3
"""mysqlmig - a simple, deterministic MySQL schema migration executor for DBAs.

Single-file tool, Python 3.12+, standard library only. SQL execution is
delegated to the existing `mysql` command-line client. See DESIGN.md for the
full design.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from typing import ClassVar

__version__ = "0.1.0"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class MysqlmigError(Exception):
    """Base class for all mysqlmig errors.

    Any MysqlmigError raised to the top level is reported as a human-readable
    message and causes exit code 1.
    """


class MigrationFormatError(MysqlmigError):
    """A migration filename or version does not match the required format."""


class MigrationDiscoveryError(MysqlmigError):
    """A problem was found while discovering/validating a migration directory."""


class MysqlClientError(MysqlmigError):
    """The mysql client could not be invoked or a metadata query failed."""


# ---------------------------------------------------------------------------
# Version model
# ---------------------------------------------------------------------------

# A version is one or more numeric components separated by dots, with an
# optional leading `v`. Examples: v1, v1.0, 1.10, v1.2.3.4
_VERSION_RE = re.compile(r"^(?P<prefix>v?)(?P<body>[0-9]+(?:\.[0-9]+)*)$")

# A migration filename is {VERSION}_{DESCRIPTION}.sql
#   - VERSION is the version grammar above (prefix + numeric body)
#   - DESCRIPTION is everything up to the trailing .sql and may contain
#     underscores, hyphens, etc. It must be non-empty.
_FILENAME_RE = re.compile(
    r"^(?P<prefix>v?)(?P<body>[0-9]+(?:\.[0-9]+)*)_(?P<description>.+)\.sql$"
)


class Version:
    """A migration version.

    Stores the version exactly as written (including the optional `v` prefix)
    while comparing numerically by component so that v1.9 < v1.10.
    """

    __slots__ = ("components", "has_prefix", "raw")

    def __init__(self, raw):
        match = _VERSION_RE.match(raw)
        if not match:
            raise MigrationFormatError(
                f"Invalid version: {raw!r}"
            )
        self.raw = raw
        self.has_prefix = match.group("prefix") == "v"
        self.components = tuple(int(c) for c in match.group("body").split("."))

    # -- identity / ordering -------------------------------------------------
    # Equality and ordering are purely numeric (component-wise). Two versions
    # that resolve to the same numeric components are equal even if written
    # differently (e.g. "v1.10" and "1.10"); this is what makes them a
    # duplicate within a single migration directory.

    def __eq__(self, other):
        if not isinstance(other, Version):
            return NotImplemented
        return self.components == other.components

    def __hash__(self):
        return hash(self.components)

    def __lt__(self, other):
        if not isinstance(other, Version):
            return NotImplemented
        return self.components < other.components

    def __le__(self, other):
        return self == other or self < other

    def __gt__(self, other):
        if not isinstance(other, Version):
            return NotImplemented
        return other < self

    def __ge__(self, other):
        return self == other or self > other

    def __repr__(self):
        return f"Version({self.raw!r})"

    def __str__(self):
        return self.raw


# ---------------------------------------------------------------------------
# Migration model
# ---------------------------------------------------------------------------

class Migration:
    """A single migration file discovered on disk."""

    __slots__ = ("description", "filename", "path", "version")

    def __init__(self, version, description, filename, path):
        self.version = version
        self.description = description
        self.filename = filename
        self.path = path

    def __repr__(self):
        return f"Migration({self.filename!r})"


def parse_migration_filename(filename, path=None):
    """Parse a migration filename into a Migration, or return None.

    Returns None when the filename does not match the migration format at all
    (such files are ignored during discovery rather than treated as errors).
    """
    match = _FILENAME_RE.match(filename)
    if not match:
        return None
    version = Version(match.group("prefix") + match.group("body"))
    description = match.group("description")
    return Migration(version, description, filename, path)


# ---------------------------------------------------------------------------
# Discovery and validation
# ---------------------------------------------------------------------------

def discover_migrations(directory):
    """Discover and validate migrations in a directory.

    Returns a list of Migration objects sorted by numeric version order.

    Raises:
        MigrationDiscoveryError - directory missing, duplicate versions, or a
            mixed version-prefix convention.
    """
    if not os.path.isdir(directory):
        raise MigrationDiscoveryError(
            f"Migration directory {directory!r} does not exist."
        )

    try:
        entries = sorted(os.listdir(directory))
    except OSError as exc:
        raise MigrationDiscoveryError(
            f"Cannot read migration directory {directory!r}: "
            f"{exc.strerror or exc}"
        ) from exc

    migrations = []
    for entry in entries:
        full = os.path.join(directory, entry)
        if not os.path.isfile(full):
            continue
        migration = parse_migration_filename(entry, full)
        if migration is None:
            # Not a migration file; ignore it.
            continue
        migrations.append(migration)

    _check_duplicate_versions(migrations)
    _check_prefix_convention(migrations)

    migrations.sort(key=lambda m: m.version.components)
    return migrations


def _check_duplicate_versions(migrations):
    seen = {}
    for migration in migrations:
        key = migration.version.components
        if key in seen:
            raise MigrationDiscoveryError(
                f"Duplicate migration version {migration.version}: {seen[key].filename!r} and {migration.filename!r} resolve to the "
                "same version."
            )
        seen[key] = migration


def _check_prefix_convention(migrations):
    """The first migration (by version order) sets the prefix convention."""
    if not migrations:
        return
    ordered = sorted(migrations, key=lambda m: m.version.components)
    expected_prefix = ordered[0].version.has_prefix
    for migration in ordered:
        if migration.version.has_prefix != expected_prefix:
            style = "with the 'v' prefix" if expected_prefix else "without the 'v' prefix"
            raise MigrationDiscoveryError(
                f"Inconsistent version prefix: {migration.filename!r} does not match the "
                f"convention established by the first migration ({style})."
            )


# ---------------------------------------------------------------------------
# MySQL client wrapper
# ---------------------------------------------------------------------------

# Fixed options applied to the migration mysql process. See DESIGN.md 13.3.
#   -vvv       maximum client verbosity so complete output is captured
#   --comments preserve SQL comments (important for stored programs)
# mysqlmig must never pass --force: a SQL error must stop the migration.
_FIXED_MIGRATION_OPTIONS = ["-vvv", "--comments"]


class ConnectionConfig:
    """Connection-related options forwarded to the mysql client.

    These mirror the DESIGN.md categories:
      * option-file options (--defaults-file / --defaults-extra-file) which are
        both interpreted by mysqlmig and forwarded FIRST on the mysql command
        line, as the client requires; and
      * connection options (host/port/user/login-path) plus the database.

    Any attribute left as None is simply omitted, so the mysql client falls back
    to its own option-file configuration (e.g. ~/.my.cnf, [client]).
    """

    __slots__ = (
        "database",
        "defaults_extra_file",
        "defaults_file",
        "host",
        "login_path",
        "port",
        "user",
    )

    def __init__(
        self,
        defaults_file=None,
        defaults_extra_file=None,
        host=None,
        port=None,
        user=None,
        login_path=None,
        database=None,
    ):
        self.defaults_file = defaults_file
        self.defaults_extra_file = defaults_extra_file
        self.host = host
        self.port = port
        self.user = user
        self.login_path = login_path
        self.database = database

    def option_file_args(self):
        """Args that MUST appear first on the mysql command line."""
        args = []
        if self.defaults_file is not None:
            args.append("--defaults-file=" + self.defaults_file)
        if self.defaults_extra_file is not None:
            args.append("--defaults-extra-file=" + self.defaults_extra_file)
        return args

    def connection_args(self):
        """Connection args (host/port/user/login-path and the database)."""
        args = []
        if self.login_path is not None:
            args.append("--login-path=" + self.login_path)
        if self.host is not None:
            args.append("--host=" + self.host)
        if self.port is not None:
            args.append("--port=" + str(self.port))
        if self.user is not None:
            args.append("--user=" + self.user)
        if self.database is not None:
            args.append("--database=" + self.database)
        return args


class MysqlResult:
    """Result of running the mysql client once."""

    __slots__ = ("output", "returncode")

    def __init__(self, returncode, output):
        self.returncode = returncode
        # Combined stdout+stderr, as captured for execution_log / parsing.
        self.output = output

    @property
    def ok(self):
        return self.returncode == 0


class MysqlClient:
    """Thin wrapper around the system `mysql` command-line client.

    Every call spawns a fresh, short-lived mysql process. mysqlmig does not hold
    a persistent connection open (see DESIGN.md Concurrency Protection).
    """

    def __init__(self, config, mysql_path="mysql"):
        self.config = config
        self.mysql_path = mysql_path

    # -- command construction ------------------------------------------------

    def _require_client(self):
        if shutil.which(self.mysql_path) is None:
            raise MysqlClientError(
                f"The '{self.mysql_path}' command-line client was not found on PATH."
            )

    def _migration_command(self):
        """mysql invocation used to run a migration file via stdin."""
        return [
            self.mysql_path,
            *self.config.option_file_args(),
            *self.config.connection_args(),
            *_FIXED_MIGRATION_OPTIONS,
        ]

    def _metadata_command(self, extra_options):
        """mysql invocation used for service/metadata SQL via stdin.

        Metadata SQL is run in batch mode so results are tab-separated and
        easy to parse. extra_options lets callers tune output (e.g. whether to
        include column headers).
        """
        return [
            self.mysql_path,
            *self.config.option_file_args(),
            *self.config.connection_args(),
            *extra_options,
        ]

    # -- execution -----------------------------------------------------------

    def run_migration(self, sql_text):
        """Run one migration's SQL, returning a MysqlResult with combined output.

        stdout and stderr are merged so the execution log preserves the natural
        interleaving of SQL echo and error messages.
        """
        self._require_client()
        proc = subprocess.run(
            self._migration_command(),
            input=sql_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return MysqlResult(proc.returncode, proc.stdout)

    def execute(self, sql_text, show_warnings=False):
        """Execute metadata SQL that returns no rows (DDL, INSERT/UPDATE).

        With show_warnings=True, the client is run with --show-warnings so
        server notes/warnings (e.g. "Table already exists") appear in the
        returned output. Raises MysqlClientError on a non-zero exit, surfacing
        the client output. Returns the combined stdout+stderr.
        """
        self._require_client()
        options = ["--batch"]
        if show_warnings:
            options.append("--show-warnings")
        proc = subprocess.run(
            self._metadata_command(options),
            input=sql_text,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise MysqlClientError(
                "mysql command failed:\n" + (proc.stderr or proc.stdout).strip()
            )
        # Warnings are printed to stderr by the client; include both streams.
        return (proc.stdout or "") + (proc.stderr or "")

    def query(self, sql_text):
        """Run a SELECT and return parsed rows.

        Uses --batch --skip-column-names so output is tab-separated with no
        header. Returns a list of rows, each a list of string column values.
        A literal NULL is represented by the mysql client as the string "NULL";
        callers that need to distinguish it should do so explicitly.
        """
        self._require_client()
        proc = subprocess.run(
            self._metadata_command(["--batch", "--skip-column-names"]),
            input=sql_text,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise MysqlClientError(
                "mysql query failed:\n" + (proc.stderr or proc.stdout).strip()
            )
        return _parse_batch_rows(proc.stdout)


def _parse_batch_rows(output):
    """Parse mysql --batch --skip-column-names output into rows of columns.

    Each non-empty line is one row; columns are tab-separated. Trailing newline
    is ignored. Empty output yields an empty list.
    """
    rows = []
    for line in output.split("\n"):
        if line == "":
            continue
        rows.append(line.split("\t"))
    return rows


# ---------------------------------------------------------------------------
# Metadata (schema_version table + v_schema_version view)
# ---------------------------------------------------------------------------

METADATA_TABLE = "schema_version"
METADATA_VIEW = "v_schema_version"

STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"

_CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS schema_version (
    id              INT UNSIGNED   NOT NULL AUTO_INCREMENT,
    version         VARCHAR(32)    NOT NULL,
    script_name     VARCHAR(255)   NOT NULL,
    status          ENUM('IN_PROGRESS', 'SUCCESS', 'FAILED') NOT NULL,
    execution_start TIMESTAMP(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    execution_time  VARCHAR(16)    NULL,
    execution_log   LONGTEXT       NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_schema_version_version (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_CREATE_VIEW_SQL = """\
CREATE OR REPLACE VIEW v_schema_version AS
SELECT
    id,
    version,
    script_name,
    status,
    execution_start,
    execution_time
FROM schema_version;
"""

# SQL expression that formats the elapsed time from execution_start to NOW(3)
# as a human-readable HH:MM:SS.mmm string (millisecond precision, matching the
# TIMESTAMP(3) columns), e.g. "00:00:12.345". Computed entirely in the database
# so a single clock is used.
#
# Built from native time functions:
#   TIMESTAMPDIFF(MICROSECOND, start, NOW(3)) -> elapsed microseconds
#   / 1000000                                 -> fractional seconds
#   SEC_TO_TIME(...)                          -> HH:MM:SS.ffffff
#   TIME_FORMAT(..., '%H:%i:%s.%f')           -> always 6 fractional digits
#   LEFT(..., 12)                             -> trim to HH:MM:SS.mmm (3 digits)
#
# Note: SEC_TO_TIME caps at the TIME range (838:59:59), which far exceeds any
# realistic migration duration.
_EXECUTION_TIME_EXPR = (
    "LEFT("
    "TIME_FORMAT("
    "SEC_TO_TIME("
    "TIMESTAMPDIFF(MICROSECOND, execution_start, NOW(3)) / 1000000"
    "), '%H:%i:%s.%f'), 12)"
)


def _sql_quote(value):
    """Return a safely quoted SQL string literal for the given Python string.

    mysqlmig has no database driver, so metadata statements are built as text.
    Only a small set of trusted-ish fields are interpolated (version,
    script_name, and captured client output), but they are still escaped
    defensively. Backslash must be escaped first.
    """
    if value is None:
        return "NULL"
    text = str(value)
    text = text.replace("\\", "\\\\")
    text = text.replace("'", "\\'")
    # Guard against accidental statement termination / NUL in log text.
    text = text.replace("\x00", "")
    return "'" + text + "'"


class Metadata:
    """Reads and writes the schema_version metadata via the mysql client."""

    def __init__(self, client):
        self.client = client

    # -- existence / creation ------------------------------------------------

    def exists(self):
        """True if the schema_version table exists in the target database."""
        rows = self.client.query(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() "
            f"AND table_name = '{METADATA_TABLE}';"
        )
        return bool(rows) and rows[0][0] != "0"

    def create(self):
        """Create the table and view in the target database (idempotent).

        The database itself is NOT created; it must already exist and be
        accessible to the connecting user (a dedicated migration user typically
        lacks CREATE DATABASE). If the database is missing or inaccessible, the
        underlying mysql client errors and MysqlClientError propagates.

        Returns True if the schema_version table already existed (detected via
        the server's "table already exists" note under --show-warnings),
        False if it was created now.
        """
        table_output = self.client.execute(_CREATE_TABLE_SQL, show_warnings=True)
        self.client.execute(_CREATE_VIEW_SQL)
        # MySQL emits Note 1050 "Table '...' already exists" for
        # CREATE TABLE IF NOT EXISTS when the table is already present.
        return "1050" in table_output or "already exists" in table_output.lower()

    # -- reads ---------------------------------------------------------------

    def load_records(self):
        """Return all records ordered by id as a list of MigrationRecord."""
        rows = self.client.query(
            "SELECT id, version, script_name, status, "
            "execution_start, execution_time "
            "FROM schema_version ORDER BY id;"
        )
        return [MigrationRecord.from_row(row) for row in rows]

    # -- writes --------------------------------------------------------------

    def insert_in_progress(self, version, script_name):
        """Insert a new IN_PROGRESS record for a migration about to run.

        execution_start is populated by the database default; execution_time
        and execution_log are left NULL.
        """
        self.client.execute(
            "INSERT INTO schema_version (version, script_name, status) "
            f"VALUES ({_sql_quote(version)}, {_sql_quote(script_name)}, '{STATUS_IN_PROGRESS}');"
        )

    def reset_in_progress(self, version, script_name):
        """Reset an existing (blocking) record to IN_PROGRESS for a retry.

        Resets execution_start to now and clears execution_time; the existing
        execution_log is preserved so the new attempt can be appended to it.
        """
        self.client.execute(
            "UPDATE schema_version SET "
            f"status = '{STATUS_IN_PROGRESS}', "
            f"script_name = {_sql_quote(script_name)}, "
            "execution_start = CURRENT_TIMESTAMP(3), "
            "execution_time = NULL "
            f"WHERE version = {_sql_quote(version)};"
        )

    def finalize(self, version, status, log_text, append=False):
        """Update a record to its final status with log and DB-computed time.

        If append is True, the new log text is concatenated onto any existing
        execution_log (used for retries); otherwise it replaces it.
        """
        if status not in (STATUS_SUCCESS, STATUS_FAILED):
            raise ValueError("finalize status must be SUCCESS or FAILED")

        if append:
            log_expr = f"CONCAT(COALESCE(execution_log, ''), {_sql_quote(log_text)})"
        else:
            log_expr = _sql_quote(log_text)

        self.client.execute(
            "UPDATE schema_version SET "
            f"status = '{status}', "
            f"execution_log = {log_expr}, "
            f"execution_time = {_EXECUTION_TIME_EXPR} "
            f"WHERE version = {_sql_quote(version)};"
        )


class MigrationRecord:
    """One row of schema_version (without the large execution_log)."""

    __slots__ = (
        "execution_start",
        "execution_time",
        "id",
        "script_name",
        "status",
        "version",
    )

    def __init__(self, id, version, script_name, status,
                 execution_start, execution_time):
        self.id = id
        self.version = version
        self.script_name = script_name
        self.status = status
        self.execution_start = execution_start
        self.execution_time = execution_time

    @classmethod
    def from_row(cls, row):
        # row: [id, version, script_name, status, execution_start, execution_time]
        execution_time = row[5]
        if execution_time == "NULL":
            execution_time = None
        return cls(
            id=int(row[0]),
            version=Version(row[1]),
            script_name=row[2],
            status=row[3],
            execution_start=row[4],
            execution_time=execution_time,
        )

    @property
    def is_blocking(self):
        return self.status != STATUS_SUCCESS

    def __repr__(self):
        return f"MigrationRecord({self.version} {self.status})"


# ---------------------------------------------------------------------------
# Migration state calculation
# ---------------------------------------------------------------------------

class MigrationStateError(MysqlmigError):
    """Repository/database inconsistency or an invalid migration state."""


class TargetError(MysqlmigError):
    """The requested target version is invalid."""


LATEST = "latest"


class MigrationState:
    """Derived view of migration state from the repository and the database.

    Combines the discovered migrations (repository) with the recorded history
    (database) and exposes current version, blocking record, latest, and the
    pending plan for a given target.
    """

    def __init__(self, migrations, records):
        # Sort defensively so derived state never depends on caller ordering.
        self.migrations = sorted(migrations, key=lambda m: m.version.components)
        self.records = list(records)                # db, ordered by id
        self._validate_consistency()

    # -- consistency ---------------------------------------------------------

    def _validate_consistency(self):
        """Verify the database history is consistent with the repository.

        Raises MigrationStateError when:
          * a SUCCESS record has no matching migration file (history references
            a migration missing from the repository), or
          * the database is ahead of the repository.
        """
        repo_by_components = {m.version.components: m for m in self.migrations}

        for record in self.records:
            if record.status != STATUS_SUCCESS:
                continue
            if record.version.components not in repo_by_components:
                raise MigrationStateError(
                    f"Database has a successful migration {record.version} that does not "
                    "exist in the repository."
                )

        # Database ahead of repository: current successful version is newer
        # than the newest migration file available.
        current = self.current_version()
        if current is not None and self.migrations:
            latest_repo = self.migrations[-1].version
            if current > latest_repo:
                raise MigrationStateError(
                    "Database is ahead of the repository "
                    f"(current {current} > latest available {latest_repo})."
                )

    # -- derived state -------------------------------------------------------

    def current_version(self):
        """The latest version with status SUCCESS, or None."""
        current = None
        for record in self.records:
            if record.status == STATUS_SUCCESS:
                if current is None or record.version > current:
                    current = record.version
        return current

    def blocking_record(self):
        """The single non-SUCCESS record, or None.

        By design there is at most one and it is the latest record. If the data
        somehow violates that, the newest non-SUCCESS record is returned.
        """
        blocking = None
        for record in self.records:
            if record.status != STATUS_SUCCESS:
                blocking = record
        return blocking

    def latest_version(self):
        """The highest available migration version in the repository, or None."""
        if not self.migrations:
            return None
        return self.migrations[-1].version

    def resolve_target(self, target):
        """Resolve a target spec (version string or 'latest') to a Version.

        Raises TargetError if the target does not exist in the repository or is
        lower than the current successful version.
        """
        if target is None or target == LATEST:
            latest = self.latest_version()
            if latest is None:
                raise TargetError("No migrations available in the repository.")
            return latest

        try:
            wanted = Version(target)
        except MigrationFormatError as exc:
            raise TargetError(str(exc)) from exc

        match = None
        for migration in self.migrations:
            if migration.version == wanted:
                match = migration.version
                break
        if match is None:
            raise TargetError(
                f"Target version {target} does not exist in the repository."
            )

        current = self.current_version()
        if current is not None and match < current:
            raise TargetError(
                f"Target version {match} is lower than the current successful "
                f"version {current}."
            )
        return match

    def pending(self, target):
        """Migrations to execute to reach target: version > current, <= target.

        Returned in numeric version order. Does not consider blocking state;
        callers must resolve a blocking record first.
        """
        resolved = self.resolve_target(target)
        current = self.current_version()
        result = []
        for migration in self.migrations:
            if current is not None and migration.version <= current:
                continue
            if migration.version > resolved:
                continue
            result.append(migration)
        return result

    def migration_for(self, version):
        """Return the repository Migration matching a Version, or None."""
        for migration in self.migrations:
            if migration.version == version:
                return migration
        return None


# ---------------------------------------------------------------------------
# Configuration ([mysqlmig] option group + directory defaulting)
# ---------------------------------------------------------------------------

class Config:
    """Resolved mysqlmig settings after merging CLI, option files, defaults."""

    __slots__ = ("connection", "database", "directory", "target", "verbose")

    def __init__(self, database, directory, connection,
                 target=None, verbose=False):
        self.database = database
        self.directory = directory
        self.connection = connection
        self.target = target
        self.verbose = verbose


def _read_option_group(group_name, option_file_args):
    """Read a named option group via my_print_defaults into a dict.

    my_print_defaults resolves the same option files the mysql client reads
    (honoring --defaults-file / --defaults-extra-file) and prints a group's
    settings as `--key=value` lines. Best-effort and tolerant: if the tool is
    unavailable or fails, an empty dict is returned.
    """
    if shutil.which("my_print_defaults") is None:
        return {}
    cmd = ["my_print_defaults", *option_file_args, group_name]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError:
        return {}
    if proc.returncode != 0:
        return {}
    result = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("--"):
            continue
        body = line[2:]
        if "=" in body:
            key, value = body.split("=", 1)
        else:
            key, value = body, ""
        result[key.strip()] = value.strip()
    return result


def _read_mysqlmig_group(option_file_args):
    """Read the [mysqlmig] option group (db / dir / login-path)."""
    return _read_option_group("mysqlmig", option_file_args)


def _host_from_print_defaults(option_file_args, mysql_path="mysql"):
    """Return the host the mysql client would use, from `mysql --print-defaults`.

    `--print-defaults` must be the first argument, so any --defaults-file /
    --defaults-extra-file flags are placed before it. The command prints the
    effective options as a single `--key=value ...` line; we scan it for the
    last `--host=` (later options win in the client). Returns None if no host
    is configured or the command cannot be run.
    """
    if shutil.which(mysql_path) is None:
        return None
    # Option-file flags must precede --print-defaults.
    cmd = [mysql_path, *option_file_args, "--print-defaults"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError:
        return None
    if proc.returncode != 0:
        return None

    host = None
    for token in proc.stdout.split():
        if token.startswith("--host="):
            host = token[len("--host="):]
    return host or None


def resolve_display_host(connection):
    """Describe the host mysqlmig connects to, for display.

    Reflects the *configured* host (what the DBA actually used), never the
    server's own @@hostname (which, e.g. behind an Aurora endpoint, is a
    meaningless instance identifier).

    Priority:
      1. An explicit --host on the ConnectionConfig.
      2. The host resolved from the mysql client's option files via
         `mysql --print-defaults` (honoring --defaults-file / extra-file).
      3. 'localhost' when no host is configured (the mysql client's default).
    """
    if connection.host:
        return connection.host

    option_file_args = []
    if connection.defaults_file is not None:
        option_file_args.append("--defaults-file=" + connection.defaults_file)
    if connection.defaults_extra_file is not None:
        option_file_args.append(
            "--defaults-extra-file=" + connection.defaults_extra_file)

    host = _host_from_print_defaults(option_file_args)
    return host if host else "localhost"


# Top-level folder that holds per-database migration subfolders.
MIGRATIONS_BASE = "migrations"


def resolve_directory(explicit_dir, database, db_from_cli):
    """Resolve the migration directory.

    Rules:
      1. An explicit directory (--dir or [mysqlmig] dir) is used literally,
         regardless of the database name.
      2. Otherwise, if the database was given explicitly on the command line
         (--db), the directory is 'migrations/<database>'.
      3. Otherwise (the database came from the [mysqlmig] default), the
         directory is the base 'migrations' folder itself, with no subfolder.
    """
    if explicit_dir:
        return explicit_dir
    if db_from_cli:
        return os.path.join(MIGRATIONS_BASE, database)
    return MIGRATIONS_BASE


# ---------------------------------------------------------------------------
# Color output
# ---------------------------------------------------------------------------

class Color:
    """Minimal ANSI color helper.

    Coloring is enabled only when stdout is a TTY and the NO_COLOR environment
    variable is unset (https://no-color.org/). When disabled, every wrapper is
    a no-op returning the plain text, so callers can colorize unconditionally.

    The enabled state is computed once at construction but can be overridden
    (e.g. in tests) via enabled=.
    """

    _CODES: ClassVar[dict] = {
        "reset": "\033[0m",
        "bold": "\033[1m",
        "red": "\033[31m",
        "green": "\033[32m",
        "yellow": "\033[33m",
        "blue": "\033[34m",
        "magenta": "\033[35m",
        "cyan": "\033[36m",
        "white": "\033[37m",
        "bright_blue": "\033[94m",
    }

    def __init__(self, enabled=None):
        if enabled is None:
            enabled = self._auto_enabled()
        self.enabled = enabled

    @staticmethod
    def _auto_enabled():
        import sys
        if os.environ.get("NO_COLOR") is not None:
            return False
        stream = sys.stdout
        try:
            return bool(stream.isatty())
        except (AttributeError, ValueError):
            return False

    def _wrap(self, text, *names):
        if not self.enabled:
            return text
        prefix = "".join(self._CODES[n] for n in names)
        return prefix + text + self._CODES["reset"]

    # Column palette. Each logical column has ONE color used everywhere it
    # appears (status, history, run, dry-run), so columns read consistently
    # across the whole app:
    #   version  -> cyan
    #   script   -> bright blue   (also used for the pending list in status)
    #   status   -> by meaning (SUCCESS green / FAILED red / IN_PROGRESS yellow)
    def version(self, text):
        return self._wrap(str(text), "cyan")

    def database(self, text):
        return self._wrap(str(text), "bold", "white")

    def script(self, text):
        return self._wrap(str(text), "bright_blue")

    # Alias: pending migrations are shown by script name, same color as the
    # SCRIPT column.
    def pending(self, text):
        return self.script(text)

    def success(self, text):
        return self._wrap(str(text), "green")

    def failure(self, text):
        return self._wrap(str(text), "red")

    def warning(self, text):
        return self._wrap(str(text), "yellow")

    def heading(self, text):
        return self._wrap(str(text), "bold")

    def heading_white(self, text):
        return self._wrap(str(text), "bold", "white")

    def rule(self, width=60, char="-"):
        """A horizontal separator line (bold when color is enabled)."""
        return self._wrap(char * width, "bold")

    def status(self, status_value, text=None):
        """Color a status by its meaning.

        status_value selects the color; text is what gets colored (defaults to
        status_value). Passing a pre-padded text lets callers keep column
        alignment while still coloring the whole cell.
        """
        if text is None:
            text = status_value
        if status_value == STATUS_SUCCESS:
            return self.success(text)
        if status_value == STATUS_FAILED:
            return self.failure(text)
        if status_value == STATUS_IN_PROGRESS:
            return self.warning(text)
        return text


# Module-level instance used by the output paths. Recomputed in main() so the
# TTY/NO_COLOR decision reflects the real runtime environment.
COLOR = Color()


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _print(message="", stream=None):
    import sys
    print(message, file=stream or sys.stdout)


def _eprint(message=""):
    import sys
    print(message, file=sys.stderr)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

class Runner:
    """Executes the status/history/run commands against a configured target.

    interactive controls whether blocking retries and metadata creation may
    prompt. In non-interactive mode they must stop with an error instead.
    """

    def __init__(self, config, interactive=False):
        self.config = config
        self.interactive = interactive
        self.client = MysqlClient(config.connection)
        self.metadata = Metadata(self.client)

    # -- helpers -------------------------------------------------------------

    def _require_initialized(self):
        if not self.metadata.exists():
            raise MysqlmigError(
                f"{METADATA_TABLE} does not exist in database "
                f"'{self.config.database}'.\n"
                "The database has not been initialized by mysqlmig. "
                "Run 'mysqlmig init' first."
            )

    def _load_state(self):
        migrations = discover_migrations(self.config.directory)
        records = self.metadata.load_records()
        return MigrationState(migrations, records)

    def _confirm(self, prompt):
        """Ask a yes/no question. Only called in interactive mode."""
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            return False
        return answer in ("y", "yes")

    # -- init ----------------------------------------------------------------

    def init(self):
        """Create the metadata objects (schema_version table + view).

        The target database must already exist and be accessible; mysqlmig does
        not create it (a dedicated migration user typically lacks CREATE
        DATABASE privilege). If the database is missing or inaccessible, the
        mysql client reports the error and init stops with a non-zero exit.

        Creation is idempotent (CREATE TABLE/VIEW IF NOT EXISTS). Running init
        on an already-initialized database succeeds and reports a warning.
        """
        already = self.metadata.create()
        _print(f"Database: {COLOR.database(self.config.database)}")
        if already:
            _print(COLOR.warning(
                "Already initialized: schema_version and v_schema_version "
                "are already present."))
        else:
            _print(COLOR.success(
                "Initialized: created schema_version and v_schema_version."))
        return 0

    # -- status --------------------------------------------------------------

    def status(self):
        self._require_initialized()
        state = self._load_state()
        current = state.current_version()
        latest = state.latest_version()
        blocking = state.blocking_record()
        target = state.resolve_target(self.config.target)
        pending = state.pending(self.config.target)

        _print(f"Database: {COLOR.database(self.config.database)}")
        _print("Current:  {}".format(
            COLOR.version(current) if current else "(none)"))
        _print(f"Target:   {COLOR.version(target)}")
        _print("Latest:   {}".format(
            COLOR.version(latest) if latest else "(none)"))
        _print()

        if blocking is not None:
            # The blocking migration is reported on its own line; it is also
            # the first thing a run must resolve, so it is excluded from the
            # plain "pending" list below to avoid listing it twice.
            _print(f"Blocking migration: {COLOR.version(blocking.version)} ({COLOR.status(blocking.status)})")
            _print()
            remaining = [m for m in pending
                         if m.version != blocking.version]
            if remaining:
                _print("Blocked migrations (cannot run until the blocking "
                       "migration succeeds):")
                for migration in remaining:
                    _print(f"  {COLOR.pending(migration.filename)}")
            return 0

        if pending:
            _print("Pending migrations:")
            for migration in pending:
                _print(f"  {COLOR.pending(migration.filename)}")
        else:
            _print("No pending migrations.")
        return 0

    # -- history -------------------------------------------------------------

    def history(self):
        self._require_initialized()
        records = self.metadata.load_records()
        if not records:
            _print("No migrations recorded.")
            return 0

        rows = [
            (
                str(r.id),
                str(r.version),
                r.script_name,
                r.status,
                r.execution_start,
                r.execution_time if r.execution_time else "-",
            )
            for r in records
        ]

        # Column widths size to the data, but SCRIPT is capped so a very long
        # filename cannot break the layout; it is truncated with an ellipsis
        # to fit the terminal width.
        headers = ("ID", "VERSION", "SCRIPT", "STATUS", "EXECUTION START", "TIME")
        w_id = max(len(headers[0]), *(len(r[0]) for r in rows))
        w_ver = max(len(headers[1]), *(len(r[1]) for r in rows))
        w_status = max(len(headers[3]), *(len(r[3]) for r in rows))
        w_start = max(len(headers[4]), *(len(r[4]) for r in rows))
        w_time = max(len(headers[5]), *(len(r[5]) for r in rows))
        w_script_data = max(len(headers[2]), *(len(r[2]) for r in rows))

        # Space the fixed columns consume, plus 5 single-space gaps.
        fixed = w_id + w_ver + w_status + w_start + w_time + 5
        try:
            term_width = shutil.get_terminal_size((100, 24)).columns
        except OSError:
            term_width = 100
        # SCRIPT gets whatever is left, within sane bounds.
        avail = term_width - fixed
        w_script = max(10, min(w_script_data, avail))

        def fit(text, width):
            if len(text) <= width:
                return text
            if width <= 3:
                return text[:width]
            return text[: width - 3] + "..."  # ASCII ellipsis, locale-safe

        header = f"{headers[0].ljust(w_id)} {headers[1].ljust(w_ver)} {headers[2].ljust(w_script)} {headers[3].ljust(w_status)} {headers[4].ljust(w_start)} {headers[5]}"
        _print(COLOR.heading(header))
        for rid, ver, script, status, start, tm in rows:
            # Pad each cell to its column width BEFORE coloring, so the ANSI
            # escape sequences do not throw off column alignment.
            ver_cell = COLOR.version(ver.ljust(w_ver))
            script_cell = COLOR.script(fit(script, w_script).ljust(w_script))
            status_cell = COLOR.status(status, status.ljust(w_status))
            _print(f"{rid.ljust(w_id)} {ver_cell} {script_cell} {status_cell} {start.ljust(w_start)} {tm}")
        return 0

    # -- run -----------------------------------------------------------------

    def run(self):
        # Metadata init: create if confirmed (interactive) or automatically
        # (non-interactive), since running `run` is itself the instruction.
        if not self.metadata.exists():
            if self.interactive:
                _print(f"{METADATA_TABLE} does not exist in database '{self.config.database}'.")
                if not self._confirm("Create it now? [y/N]: "):
                    _print("Aborted.")
                    return 1
            self.metadata.create()
        else:
            # Ensure the view exists too (idempotent, cheap).
            self.metadata.create()

        state = self._load_state()

        # Resolve blocking migration first.
        blocking = state.blocking_record()
        if blocking is not None:
            if not self._handle_blocking(blocking, state):
                return 1
            # Reload state after a successful retry.
            state = self._load_state()

        pending = state.pending(self.config.target)
        target = state.resolve_target(self.config.target)

        if not pending:
            current = state.current_version()
            _print("Current: {}".format(
                COLOR.version(current) if current else "(none)"))
            _print(f"Target:  {COLOR.version(target)}")
            _print()
            _print("No pending migrations.")
            return 0

        for migration in pending:
            if not self._execute_one(migration, retry=False):
                return 1

        _print()
        _print(COLOR.success("Migration complete."))
        _print(f"Current version: {COLOR.version(pending[-1].version)}")
        return 0

    def _handle_blocking(self, blocking, state):
        """Resolve a blocking migration. Returns True to continue, False to stop."""
        migration = state.migration_for(blocking.version)
        if migration is None:
            raise MigrationStateError(
                f"Blocking migration {blocking.version} is not present in the repository; "
                "cannot retry."
            )

        if not self.interactive:
            raise MysqlmigError(
                f"Migration {migration.filename} is in state "
                f"{blocking.status}.\n"
                "The migration may have partially changed the database.\n"
                "An interactive terminal is required to confirm the retry."
            )

        _print(COLOR.warning(
            f"Migration {migration.filename} is in state {blocking.status}."))
        if blocking.status == STATUS_IN_PROGRESS:
            _print(COLOR.warning(
                "It was interrupted and may not have finished."))
        _print()
        _print(COLOR.warning(
            "The migration may have partially changed the database."))
        _print()
        if not self._confirm("Retry this migration? [y/N]: "):
            _print("Aborted.")
            return False

        return self._execute_one(migration, retry=True)

    def _execute_one(self, migration, retry):
        """Run a single migration, recording the result. Returns success bool."""
        sql_text = _read_migration_sql(migration.path)

        if retry:
            self.metadata.reset_in_progress(
                str(migration.version), migration.filename)
        else:
            self.metadata.insert_in_progress(
                str(migration.version), migration.filename)

        # In verbose mode, make each script's output a clearly delimited block
        # with a bold-white header, so successive scripts are easy to tell apart.
        if self.config.verbose:
            _print()
            _print(COLOR.heading_white(f"Executing: {migration.filename}"))
            _print(COLOR.rule())
        else:
            _print(f"Executing: {migration.filename}")

        result = self.client.run_migration(sql_text)

        status = STATUS_SUCCESS if result.ok else STATUS_FAILED
        self.metadata.finalize(
            str(migration.version), status, result.output, append=retry)

        if self.config.verbose:
            _print(result.output.rstrip("\n"))
            _print(COLOR.rule())

        if result.ok:
            _print(f"  {COLOR.success('SUCCESS')}")
            return True

        _print(f"  {COLOR.failure('FAILED')}")
        _print()
        _eprint(COLOR.failure(f"Migration failed: {migration.filename}"))
        if not self.config.verbose:
            _eprint(result.output.rstrip("\n"))
        return False


def _read_migration_sql(path):
    """Read a migration file as UTF-8. Non-UTF-8 files are invalid."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except UnicodeDecodeError as exc:
        raise MysqlmigError(
            f"Migration file {path} is not valid UTF-8."
        ) from exc
    except OSError as exc:
        raise MysqlmigError(
            f"Could not read migration file {path}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Interactive mode
# ---------------------------------------------------------------------------

HELP_TEXT = """\
mysqlmig - simple MySQL schema migration executor

Usage:
  mysqlmig [COMMAND] [OPTIONS]

Commands:
  init                       Create the metadata in an existing database
  status                     Show current migration state
  history                    Show migration history
  run                        Execute pending migrations

Run options:
  -t, --to VERSION|latest    Target version (default: latest)
  -v, --verbose              Show full mysql client output

Global options:
  -D, --db DATABASE          Target database
      --dir DIRECTORY        Migration directory
  -h, --host HOST            MySQL host
  -P, --port PORT            MySQL port
  -u, --user USER            MySQL user
  -L, --login-path NAME      MySQL login path
      --help                 Show this help
      --version              Show version
      --defaults-file=FILE
                             Read only this MySQL option file
      --defaults-extra-file=FILE
                             Read this option file in addition to the defaults

Options may be given before or after the command.
Running mysqlmig with no command opens the interactive menu.\
"""

_HELP_COMMANDS = ("init", "status", "history", "run")


def render_help():
    """Return HELP_TEXT with colorization (via COLOR).

    Coloring mirrors the look of a modern argparse help, but is applied by us
    so it is identical on every supported Python version:
      * section headings (lines ending with ':')  -> bold
      * option flags (-x / --long)                 -> cyan
      * value placeholders / metavars (UPPERCASE)  -> yellow
      * command names                              -> green
    When color is disabled, every wrapper is a no-op, so the text is plain.
    """
    out = []
    for line in HELP_TEXT.split("\n"):
        stripped = line.rstrip()
        if stripped and not line.startswith(" ") and stripped.endswith(":"):
            out.append(COLOR.heading(stripped))
        elif line.startswith("  "):
            out.append(_colorize_help_line(line))
        else:
            out.append(line)
    return "\n".join(out)


def _colorize_help_line(line):
    """Colorize an indented help line (command or option entry)."""
    import re

    # Command entries: "  status   Show ..." -> color the command name green.
    stripped = line.strip()
    first = stripped.split(" ", 1)[0]
    if first in _HELP_COMMANDS:
        return line.replace(first, COLOR.success(first), 1)

    # Option entries: color flags (cyan) and UPPERCASE metavars (yellow).
    def color_flag(match):
        return COLOR.version(match.group(0))

    def color_metavar(match):
        return COLOR.warning(match.group(0))

    # Flags: -X or --long-name (optionally followed by =).
    line = re.sub(r"--?[A-Za-z][\w-]*", color_flag, line)
    # Metavars: an UPPERCASE token, optionally followed by alternatives like
    # "|latest" (so "VERSION|latest" is colored as one unit).
    line = re.sub(r"\b[A-Z]{2,}(?:\|[A-Za-z]+)?", color_metavar, line)
    return line


class InteractiveMenu:
    """The interactive menu shown when mysqlmig is run with no command.

    input_fn and output_fn are injectable for testing. A choice routes to the
    Runner (interactive=True so it may prompt for confirmations), then the menu
    is shown again until the user exits.
    """

    def __init__(self, config, input_fn=None, output_fn=None):
        self.config = config
        self.runner = Runner(config, interactive=True)
        self._input = input_fn or input
        self._output = output_fn or (lambda msg="": _print(msg))

    def _header(self):
        self._output(COLOR.heading(
            f"MySQL Migration Tool v{__version__}"))
        self._output()
        host = self._display_host()
        if host:
            self._output(f"Host:     {host}")
        self._output(f"Database: {COLOR.database(self.config.database)}")

    def _display_host(self):
        """The configured host for the header (CLI --host, option files, or
        'localhost'). Never the server's @@hostname."""
        return resolve_display_host(self.config.connection)

    def _menu(self):
        self._output()
        self._output("What would you like to do?")
        self._output()
        self._output("  1. Initialize database")
        self._output("  2. Status")
        self._output("  3. History")
        self._output("  4. Run migrations")
        self._output("  5. Help")
        self._output("  6. Exit")
        self._output()

    def run(self):
        """Run the menu loop. Returns an exit code (0)."""
        self._header()
        while True:
            self._menu()
            try:
                choice = self._input("Select: ").strip()
            except EOFError:
                self._output()
                return 0

            if choice == "1":
                self._safe(self.runner.init)
            elif choice == "2":
                self._safe(self.runner.status)
            elif choice == "3":
                self._safe(self.runner.history)
            elif choice == "4":
                self._safe(self._run_with_prompt)
            elif choice == "5":
                self._output()
                self._output(render_help())
                self._separator()
            elif choice == "6" or choice.lower() in ("exit", "quit", "q"):
                return 0
            else:
                self._output(f"Invalid choice: {choice!r}")

    def _separator(self):
        """Visual divider shown between command output and the next menu."""
        self._output()
        self._output(COLOR.heading("-" * 60))

    def _run_with_prompt(self):
        """Show the current state and pending migrations, then run.

        Seeing current/latest/pending first lets the DBA choose a target
        without remembering the available versions. The target prompt is
        skipped when there is nothing to choose: no pending migrations, or an
        empty repository.
        """
        if self.runner.metadata.exists():
            # Initialized: show full status, then decide based on pending.
            self.runner.status()
            self._output()
            state = self.runner._load_state()
            pending = state.pending(LATEST)
            if not pending:
                # Already at the latest version; status() above already
                # reported "No pending migrations", so just return without
                # prompting or running.
                return
        else:
            # Fresh database: no table yet, so list the repo migrations (all of
            # which are pending) and note that running will create the table.
            self._output(f"{METADATA_TABLE} does not exist in database '{self.config.database}' yet; "
                         "running will create it.")
            self._output()
            migrations = discover_migrations(self.config.directory)
            if not migrations:
                self._output(f"No migrations found in '{self.config.directory}'.")
                return
            self._output("Pending migrations:")
            for migration in migrations:
                self._output(f"  {COLOR.pending(migration.filename)}")
            self._output()

        try:
            answer = self._input("Target version [latest]: ").strip()
        except EOFError:
            answer = ""
        # Blank or 'latest' means the latest available migration.
        target = answer if answer else LATEST
        # Apply the chosen target for this run only.
        previous = self.config.target
        self.config.target = target
        try:
            self.runner.run()
        finally:
            self.config.target = previous

    def _safe(self, action):
        """Run a command action, reporting mysqlmig errors without exiting."""
        self._output()
        try:
            action()
        except MysqlmigError as exc:
            self._output(COLOR.failure(f"ERROR: {exc}"))
        self._separator()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _add_global_options(parser):
    """Add the global connection/selection options to a parser.

    Defaults are SUPPRESS so that, when these options also live on a subparser,
    an unspecified copy does not overwrite a value given before the command.
    -h maps to --host to match the mysql client convention.
    """
    parser.add_argument("-D", "--db", dest="db", metavar="DATABASE",
                        default=argparse.SUPPRESS)
    parser.add_argument("--dir", dest="dir", metavar="DIRECTORY",
                        default=argparse.SUPPRESS)
    parser.add_argument("-h", "--host", dest="host", metavar="HOST",
                        default=argparse.SUPPRESS)
    parser.add_argument("-P", "--port", dest="port", metavar="PORT",
                        default=argparse.SUPPRESS)
    parser.add_argument("-u", "--user", dest="user", metavar="USER",
                        default=argparse.SUPPRESS)
    parser.add_argument("-L", "--login-path", dest="login_path",
                        metavar="NAME", default=argparse.SUPPRESS)
    parser.add_argument("--defaults-file", dest="defaults_file",
                        metavar="FILE", default=argparse.SUPPRESS)
    parser.add_argument("--defaults-extra-file", dest="defaults_extra_file",
                        metavar="FILE", default=argparse.SUPPRESS)


def _add_run_options(parser):
    """Add the run-only options to a parser (also SUPPRESS defaults)."""
    parser.add_argument("-t", "--to", dest="to", metavar="VERSION|latest",
                        default=argparse.SUPPRESS)
    parser.add_argument("-v", "--verbose", dest="verbose", action="store_true",
                        default=argparse.SUPPRESS)


# --help and --version are handled in main() before argparse so we can render
# colored help ourselves. The parser is built with add_help=False; -h maps to
# --host, not help.
def build_parser():
    parser = argparse.ArgumentParser(
        prog="mysqlmig",
        description="Simple, deterministic MySQL schema migration executor.",
        add_help=False,
    )

    # Globals + run options are also placed on the subparsers (via a shared
    # parent) so they may appear either before or after the command.
    _add_global_options(parser)
    _add_run_options(parser)

    global_parent = argparse.ArgumentParser(add_help=False)
    _add_global_options(global_parent)

    # add_help=False on subparsers because -h is --host, not help; help is
    # handled in main() via render_help().
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", parents=[global_parent], add_help=False,
                   help="Create the database (if needed) and metadata")
    sub.add_parser("status", parents=[global_parent], add_help=False,
                   help="Show current migration state")
    sub.add_parser("history", parents=[global_parent], add_help=False,
                   help="Show migration history")
    run_parser = sub.add_parser("run", parents=[global_parent], add_help=False,
                                help="Execute pending migrations")
    _add_run_options(run_parser)

    return parser


def _resolve_config(args, group_reader=None):
    """Merge CLI args, [mysqlmig] option-file group, and defaults into Config.

    group_reader is an injectable callable(option_file_args)->dict used for the
    [mysqlmig] group; it defaults to the my_print_defaults-based reader. CLI
    values take precedence over the group, which takes precedence over
    built-in defaults.
    """
    if group_reader is None:
        group_reader = _read_mysqlmig_group

    # Options use SUPPRESS defaults, so an unspecified option may be absent
    # from the namespace entirely; read everything through getattr.
    cli_db = getattr(args, "db", None)
    cli_dir = getattr(args, "dir", None)
    cli_host = getattr(args, "host", None)
    cli_port = getattr(args, "port", None)
    cli_user = getattr(args, "user", None)
    cli_login_path = getattr(args, "login_path", None)
    cli_defaults_file = getattr(args, "defaults_file", None)
    cli_defaults_extra_file = getattr(args, "defaults_extra_file", None)

    option_file_args = []
    if cli_defaults_file is not None:
        option_file_args.append("--defaults-file=" + cli_defaults_file)
    if cli_defaults_extra_file is not None:
        option_file_args.append("--defaults-extra-file=" + cli_defaults_extra_file)

    group = group_reader(option_file_args)

    db_from_cli = cli_db is not None
    database = cli_db or group.get("db")
    if not database:
        raise MysqlmigError(
            "No target database specified. Use --db or set db in [mysqlmig]."
        )

    explicit_dir = cli_dir or group.get("dir")
    directory = resolve_directory(explicit_dir, database, db_from_cli)

    login_path = cli_login_path or group.get("login-path")

    connection = ConnectionConfig(
        defaults_file=cli_defaults_file,
        defaults_extra_file=cli_defaults_extra_file,
        host=cli_host,
        port=cli_port,
        user=cli_user,
        login_path=login_path,
        database=database,
    )

    return Config(
        database=database,
        directory=directory,
        connection=connection,
        target=getattr(args, "to", None),
        verbose=getattr(args, "verbose", False),
    )


def main(argv=None):
    """Entry point. Returns an exit code (0 success, 1 error)."""
    if argv is None:
        argv = sys.argv[1:]

    # Recompute color enablement against the real runtime stdout/environment
    # (the module-level instance was built at import time).
    global COLOR
    COLOR = Color()

    # Handle --help / --version ourselves so help is colored consistently
    # across Python versions (argparse only colors on 3.14+).
    if "--help" in argv:
        _print(render_help())
        return 0
    if "--version" in argv:
        _print(f"mysqlmig {__version__}")
        return 0

    parser = build_parser()
    args = parser.parse_args(argv)

    # Run-only options are meaningless without the `run` command; reject them
    # instead of silently falling into interactive mode.
    if args.command is None:
        for opt_attr, opt_name in (("to", "--to"), ("verbose", "--verbose")):
            if getattr(args, opt_attr, None) not in (None, False):
                _eprint(COLOR.failure(
                    f"ERROR: {opt_name} can only be used with the "
                    "'run' command."))
                return 1

    try:
        config = _resolve_config(args)

        # Interactive mode is selected by the ABSENCE of a command
        # (DESIGN sect 9).
        if args.command is None:
            return InteractiveMenu(config).run()

        runner = Runner(config, interactive=False)
        if args.command == "init":
            return runner.init()
        if args.command == "status":
            return runner.status()
        if args.command == "history":
            return runner.history()
        if args.command == "run":
            return runner.run()
        _eprint(f"Unknown command: {args.command}")
        return 1
    except MysqlmigError as exc:
        _eprint(COLOR.failure(f"ERROR: {exc}"))
        return 1


if __name__ == "__main__":
    sys.exit(main())
