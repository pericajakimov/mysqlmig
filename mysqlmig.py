#!/usr/bin/env python3
"""mysqlmig - a simple, deterministic MySQL schema migration executor for DBAs.

Single-file tool, Python 3.8+, standard library only. SQL execution is delegated
to the existing `mysql` command-line client. See DESIGN.md for the full design.
"""

import os
import re
import shutil
import subprocess

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

    __slots__ = ("raw", "has_prefix", "components")

    def __init__(self, raw):
        match = _VERSION_RE.match(raw)
        if not match:
            raise MigrationFormatError(
                "Invalid version: {!r}".format(raw)
            )
        self.raw = raw
        self.has_prefix = match.group("prefix") == "v"
        self.components = tuple(int(c) for c in match.group("body").split("."))

    # -- identity / ordering -------------------------------------------------
    # Equality and ordering are purely numeric (component-wise). Two versions
    # that resolve to the same numeric components are equal even if written
    # differently (e.g. "v1.10" and "1.10"); this is what makes them a
    # duplicate within a single migration directory.

    def _cmp_key(self):
        return self.components

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
        return "Version({!r})".format(self.raw)

    def __str__(self):
        return self.raw


# ---------------------------------------------------------------------------
# Migration model
# ---------------------------------------------------------------------------

class Migration:
    """A single migration file discovered on disk."""

    __slots__ = ("version", "description", "filename", "path")

    def __init__(self, version, description, filename, path):
        self.version = version
        self.description = description
        self.filename = filename
        self.path = path

    def __repr__(self):
        return "Migration({!r})".format(self.filename)


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
            "Migration directory {!r} does not exist.".format(directory)
        )

    migrations = []
    for entry in sorted(os.listdir(directory)):
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
                "Duplicate migration version {}: {!r} and {!r} resolve to the "
                "same version.".format(
                    migration.version, seen[key].filename, migration.filename
                )
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
                "Inconsistent version prefix: {!r} does not match the "
                "convention established by the first migration ({}).".format(
                    migration.filename, style
                )
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
        "defaults_file",
        "defaults_extra_file",
        "host",
        "port",
        "user",
        "login_path",
        "database",
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

    def connection_args(self, include_database=True):
        """Connection args (host/port/user/login-path and optionally database)."""
        args = []
        if self.login_path is not None:
            args.append("--login-path=" + self.login_path)
        if self.host is not None:
            args.append("--host=" + self.host)
        if self.port is not None:
            args.append("--port=" + str(self.port))
        if self.user is not None:
            args.append("--user=" + self.user)
        if include_database and self.database is not None:
            args.append("--database=" + self.database)
        return args


class MysqlResult:
    """Result of running the mysql client once."""

    __slots__ = ("returncode", "output")

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
                "The '{}' command-line client was not found on PATH.".format(
                    self.mysql_path
                )
            )

    def _migration_command(self):
        """mysql invocation used to run a migration file via stdin."""
        return (
            [self.mysql_path]
            + self.config.option_file_args()
            + self.config.connection_args(include_database=True)
            + list(_FIXED_MIGRATION_OPTIONS)
        )

    def _metadata_command(self, extra_options):
        """mysql invocation used for service/metadata SQL via stdin.

        Metadata SQL is run in batch mode so results are tab-separated and
        easy to parse. extra_options lets callers tune output (e.g. whether to
        include column headers).
        """
        return (
            [self.mysql_path]
            + self.config.option_file_args()
            + self.config.connection_args(include_database=True)
            + list(extra_options)
        )

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

    def execute(self, sql_text):
        """Execute metadata SQL that returns no rows (DDL, INSERT/UPDATE).

        Raises MysqlClientError on a non-zero exit, surfacing the client output.
        """
        self._require_client()
        proc = subprocess.run(
            self._metadata_command(["--batch"]),
            input=sql_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode != 0:
            raise MysqlClientError(
                "mysql command failed:\n" + (proc.stderr or proc.stdout).strip()
            )
        return proc.stdout

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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
            "AND table_name = '{}';".format(METADATA_TABLE)
        )
        return bool(rows) and rows[0][0] != "0"

    def create(self):
        """Create the table and view. Idempotent."""
        self.client.execute(_CREATE_TABLE_SQL)
        self.client.execute(_CREATE_VIEW_SQL)

    # -- reads ---------------------------------------------------------------

    def load_records(self):
        """Return all records ordered by id as a list of MigrationRecord."""
        rows = self.client.query(
            "SELECT id, version, script_name, status, "
            "execution_start, execution_time "
            "FROM schema_version ORDER BY id;"
        )
        return [MigrationRecord.from_row(row) for row in rows]

    def get_log(self, version):
        """Return the current execution_log for a version, or None."""
        rows = self.client.query(
            "SELECT execution_log FROM schema_version WHERE version = {};".format(
                _sql_quote(version)
            )
        )
        if not rows:
            return None
        value = rows[0][0]
        return None if value == "NULL" else value

    # -- writes --------------------------------------------------------------

    def insert_in_progress(self, version, script_name):
        """Insert a new IN_PROGRESS record for a migration about to run.

        execution_start is populated by the database default; execution_time
        and execution_log are left NULL.
        """
        self.client.execute(
            "INSERT INTO schema_version (version, script_name, status) "
            "VALUES ({}, {}, '{}');".format(
                _sql_quote(version), _sql_quote(script_name), STATUS_IN_PROGRESS
            )
        )

    def reset_in_progress(self, version, script_name):
        """Reset an existing (blocking) record to IN_PROGRESS for a retry.

        Resets execution_start to now and clears execution_time; the existing
        execution_log is preserved so the new attempt can be appended to it.
        """
        self.client.execute(
            "UPDATE schema_version SET "
            "status = '{}', "
            "script_name = {}, "
            "execution_start = CURRENT_TIMESTAMP(3), "
            "execution_time = NULL "
            "WHERE version = {};".format(
                STATUS_IN_PROGRESS, _sql_quote(script_name), _sql_quote(version)
            )
        )

    def finalize(self, version, status, log_text, append=False):
        """Update a record to its final status with log and DB-computed time.

        If append is True, the new log text is concatenated onto any existing
        execution_log (used for retries); otherwise it replaces it.
        """
        if status not in (STATUS_SUCCESS, STATUS_FAILED):
            raise ValueError("finalize status must be SUCCESS or FAILED")

        if append:
            log_expr = "CONCAT(COALESCE(execution_log, ''), {})".format(
                _sql_quote(log_text)
            )
        else:
            log_expr = _sql_quote(log_text)

        self.client.execute(
            "UPDATE schema_version SET "
            "status = '{status}', "
            "execution_log = {log_expr}, "
            "execution_time = {time_expr} "
            "WHERE version = {version};".format(
                status=status,
                log_expr=log_expr,
                time_expr=_EXECUTION_TIME_EXPR,
                version=_sql_quote(version),
            )
        )


class MigrationRecord:
    """One row of schema_version (without the large execution_log)."""

    __slots__ = (
        "id",
        "version",
        "script_name",
        "status",
        "execution_start",
        "execution_time",
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
        return "MigrationRecord({} {})".format(self.version, self.status)


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
                    "Database has a successful migration {} that does not "
                    "exist in the repository.".format(record.version)
                )

        # Database ahead of repository: current successful version is newer
        # than the newest migration file available.
        current = self.current_version()
        if current is not None and self.migrations:
            latest_repo = self.migrations[-1].version
            if current > latest_repo:
                raise MigrationStateError(
                    "Database is ahead of the repository "
                    "(current {} > latest available {}).".format(
                        current, latest_repo
                    )
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
            raise TargetError(str(exc))

        match = None
        for migration in self.migrations:
            if migration.version == wanted:
                match = migration.version
                break
        if match is None:
            raise TargetError(
                "Target version {} does not exist in the repository.".format(
                    target
                )
            )

        current = self.current_version()
        if current is not None and match < current:
            raise TargetError(
                "Target version {} is lower than the current successful "
                "version {}.".format(match, current)
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

    __slots__ = ("database", "directory", "connection", "target",
                 "dry_run", "verbose")

    def __init__(self, database, directory, connection,
                 target=None, dry_run=False, verbose=False):
        self.database = database
        self.directory = directory
        self.connection = connection
        self.target = target
        self.dry_run = dry_run
        self.verbose = verbose


def _read_mysqlmig_group(option_file_args):
    """Read the [mysqlmig] option group using my_print_defaults.

    my_print_defaults resolves the same option files the mysql client reads
    (honoring --defaults-file / --defaults-extra-file) and prints a named
    group's settings as `--key=value` lines. mysqlmig only needs db/dir/
    login-path, so this is kept best-effort and tolerant: if the tool is
    unavailable or fails, an empty dict is returned and mysqlmig falls back to
    CLI values and defaults.

    Returns a dict possibly containing 'db', 'dir', 'login-path'.
    """
    # my_print_defaults is the standard tool to dump a named option group.
    if shutil.which("my_print_defaults") is None:
        return {}
    cmd = ["my_print_defaults"]
    # my_print_defaults understands the same option-file flags.
    cmd += option_file_args
    cmd.append("mysqlmig")
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
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
                "{} does not exist in database '{}'.\n"
                "The database has not been initialized by mysqlmig.".format(
                    METADATA_TABLE, self.config.database
                )
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

    # -- status --------------------------------------------------------------

    def status(self):
        self._require_initialized()
        state = self._load_state()
        current = state.current_version()
        latest = state.latest_version()
        blocking = state.blocking_record()
        target = state.resolve_target(self.config.target)
        pending = state.pending(self.config.target)

        _print("Database: {}".format(self.config.database))
        _print("Current:  {}".format(current if current else "(none)"))
        _print("Target:   {}".format(target))
        _print("Latest:   {}".format(latest if latest else "(none)"))
        _print()

        if blocking is not None:
            # The blocking migration is reported on its own line; it is also
            # the first thing a run must resolve, so it is excluded from the
            # plain "pending" list below to avoid listing it twice.
            _print("Blocking migration: {} ({})".format(
                blocking.version, blocking.status))
            _print()
            remaining = [m for m in pending
                         if m.version != blocking.version]
            if remaining:
                _print("Blocked migrations (cannot run until the blocking "
                       "migration succeeds):")
                for migration in remaining:
                    _print("  {}".format(migration.filename))
            return 0

        if pending:
            _print("Pending migrations:")
            for migration in pending:
                _print("  {}".format(migration.filename))
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
        header = "{:<4} {:<10} {:<28} {:<12} {:<23} {}".format(
            "ID", "VERSION", "SCRIPT", "STATUS", "EXECUTION START", "TIME")
        _print(header)
        for r in records:
            _print("{:<4} {:<10} {:<28} {:<12} {:<23} {}".format(
                r.id,
                str(r.version),
                r.script_name,
                r.status,
                r.execution_start,
                r.execution_time if r.execution_time else "-",
            ))
        return 0

    # -- run -----------------------------------------------------------------

    def run(self):
        # Metadata init: create if confirmed (interactive) or automatically
        # (non-interactive), since running `run` is itself the instruction.
        if not self.metadata.exists():
            if self.interactive:
                _print("{} does not exist in database '{}'.".format(
                    METADATA_TABLE, self.config.database))
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
            _print("Current: {}".format(current if current else "(none)"))
            _print("Target:  {}".format(target))
            _print()
            _print("No pending migrations.")
            return 0

        if self.config.dry_run:
            return self._show_dry_run(state, pending, target)

        for migration in pending:
            if not self._execute_one(migration, retry=False):
                return 1

        _print()
        _print("Migration complete.")
        _print("Current version: {}".format(pending[-1].version))
        return 0

    def _handle_blocking(self, blocking, state):
        """Resolve a blocking migration. Returns True to continue, False to stop."""
        migration = state.migration_for(blocking.version)
        if migration is None:
            raise MigrationStateError(
                "Blocking migration {} is not present in the repository; "
                "cannot retry.".format(blocking.version)
            )

        if not self.interactive:
            raise MysqlmigError(
                "Migration {} previously {}.\n"
                "The migration may have partially changed the database.\n"
                "An interactive terminal is required to confirm the retry.".format(
                    migration.filename, blocking.status.lower().replace("_", " ")
                )
            )

        if blocking.status == STATUS_FAILED:
            _print("Previous migration failed:")
        else:
            _print("Previous migration did not finish:")
        _print()
        _print("  {}".format(migration.filename))
        _print()
        _print("The migration may have partially changed the database.")
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

        _print("Executing: {}".format(migration.filename))
        result = self.client.run_migration(sql_text)

        status = STATUS_SUCCESS if result.ok else STATUS_FAILED
        self.metadata.finalize(
            str(migration.version), status, result.output, append=retry)

        if self.config.verbose:
            _print()
            _print(result.output.rstrip("\n"))
            _print()

        if result.ok:
            _print("  SUCCESS")
            return True

        _print("  FAILED")
        _print()
        _eprint("Migration failed: {}".format(migration.filename))
        if not self.config.verbose:
            _eprint(result.output.rstrip("\n"))
        return False

    def _show_dry_run(self, state, pending, target):
        current = state.current_version()
        _print("Current: {}".format(current if current else "(none)"))
        _print("Target:  {}".format(target))
        _print()
        _print("Pending migrations:")
        for migration in pending:
            _print("  {}".format(migration.filename))
        _print()
        _print("Dry run: no migrations executed.")
        return 0


def _read_migration_sql(path):
    """Read a migration file as UTF-8. Non-UTF-8 files are invalid."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except UnicodeDecodeError:
        raise MysqlmigError(
            "Migration file {} is not valid UTF-8.".format(path)
        )
    except OSError as exc:
        raise MysqlmigError(
            "Could not read migration file {}: {}".format(path, exc)
        )


# ---------------------------------------------------------------------------
# Interactive mode
# ---------------------------------------------------------------------------

HELP_TEXT = """\
mysqlmig - simple MySQL schema migration executor

Commands (non-interactive):
  mysqlmig status            Show current migration state
  mysqlmig history           Show migration history
  mysqlmig run [options]     Execute pending migrations

Run options:
  -t, --to VERSION|latest    Target version (default: latest)
      --dry-run              Show the plan without executing
  -v, --verbose              Show full mysql client output

Global options:
  -D, --db DATABASE          Target database
      --dir DIRECTORY        Migration directory
  -h, --host HOST            MySQL host
  -P, --port PORT            MySQL port
  -u, --user USER            MySQL user
  -L, --login-path NAME      MySQL login path
      --defaults-file=FILE
      --defaults-extra-file=FILE
      --help                 Show this help
      --version              Show version

Running mysqlmig with no command opens this interactive menu.\
"""


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
        self._output("MySQL Migration Tool v{}".format(__version__))
        self._output()
        self._output("Database: {}".format(self.config.database))

    def _menu(self):
        self._output()
        self._output("What would you like to do?")
        self._output()
        self._output("  1. Status")
        self._output("  2. History")
        self._output("  3. Run migrations")
        self._output("  4. Help")
        self._output("  5. Exit")
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
                self._safe(self.runner.status)
            elif choice == "2":
                self._safe(self.runner.history)
            elif choice == "3":
                self._safe(self.runner.run)
            elif choice == "4":
                self._output()
                self._output(HELP_TEXT)
            elif choice == "5" or choice.lower() in ("exit", "quit", "q"):
                return 0
            else:
                self._output("Invalid choice: {!r}".format(choice))

    def _safe(self, action):
        """Run a command action, reporting mysqlmig errors without exiting."""
        self._output()
        try:
            action()
        except MysqlmigError as exc:
            self._output("ERROR: {}".format(exc))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

import argparse
import sys


_COMMANDS = ("status", "history", "run")


# -h is mapped to --host to match the mysql client convention, so the parser is
# built with add_help=False and --help is added explicitly (no -h help alias).
def build_parser():
    parser = argparse.ArgumentParser(
        prog="mysqlmig",
        description="Simple, deterministic MySQL schema migration executor.",
        add_help=False,
    )
    parser.add_argument("--help", action="help",
                        help="Show this help message and exit")
    parser.add_argument("--version", action="version",
                        version="mysqlmig {}".format(__version__))

    parser.add_argument("-D", "--db", dest="db", metavar="DATABASE")
    parser.add_argument("--dir", dest="dir", metavar="DIRECTORY")
    parser.add_argument("-h", "--host", dest="host", metavar="HOST")
    parser.add_argument("-P", "--port", dest="port", metavar="PORT")
    parser.add_argument("-u", "--user", dest="user", metavar="USER")
    parser.add_argument("-L", "--login-path", dest="login_path", metavar="NAME")
    parser.add_argument("--defaults-file", dest="defaults_file", metavar="FILE")
    parser.add_argument("--defaults-extra-file", dest="defaults_extra_file",
                        metavar="FILE")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status", help="Show current migration state", add_help=True)
    sub.add_parser("history", help="Show migration history", add_help=True)
    run_parser = sub.add_parser("run", help="Execute pending migrations",
                                add_help=True)
    run_parser.add_argument("-t", "--to", dest="to", metavar="VERSION|latest")
    run_parser.add_argument("--dry-run", dest="dry_run", action="store_true")
    run_parser.add_argument("-v", "--verbose", dest="verbose",
                            action="store_true")

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

    option_file_args = []
    if args.defaults_file is not None:
        option_file_args.append("--defaults-file=" + args.defaults_file)
    if args.defaults_extra_file is not None:
        option_file_args.append("--defaults-extra-file=" + args.defaults_extra_file)

    group = group_reader(option_file_args)

    db_from_cli = args.db is not None
    database = args.db or group.get("db")
    if not database:
        raise MysqlmigError(
            "No target database specified. Use --db or set db in [mysqlmig]."
        )

    explicit_dir = args.dir or group.get("dir")
    directory = resolve_directory(explicit_dir, database, db_from_cli)

    login_path = args.login_path or group.get("login-path")

    connection = ConnectionConfig(
        defaults_file=args.defaults_file,
        defaults_extra_file=args.defaults_extra_file,
        host=args.host,
        port=args.port,
        user=args.user,
        login_path=login_path,
        database=database,
    )

    return Config(
        database=database,
        directory=directory,
        connection=connection,
        target=getattr(args, "to", None),
        dry_run=getattr(args, "dry_run", False),
        verbose=getattr(args, "verbose", False),
    )


def main(argv=None):
    """Entry point. Returns an exit code (0 success, 1 error)."""
    if argv is None:
        argv = sys.argv[1:]

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = _resolve_config(args)

        # Interactive mode is selected by the ABSENCE of a command
        # (DESIGN sect 9).
        if args.command is None:
            return InteractiveMenu(config).run()

        runner = Runner(config, interactive=False)
        if args.command == "status":
            return runner.status()
        if args.command == "history":
            return runner.history()
        if args.command == "run":
            return runner.run()
        _eprint("Unknown command: {}".format(args.command))
        return 1
    except MysqlmigError as exc:
        _eprint("ERROR: {}".format(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
