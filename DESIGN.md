# mysqlmig Design

## 1. Project Overview

`mysqlmig` is a small, deterministic MySQL schema migration executor designed for DBAs.

It replaces a manual migration process where DBAs:

1. Create SQL migration files.
2. Commit them to Git.
3. Connect to the target MySQL database.
4. Execute the migrations manually.
5. Keep track of which migrations were applied.

`mysqlmig` automates execution and tracking while deliberately leaving SQL design and database decisions to the DBA.

The intended workflow is:

```text
Git repository
      ↓
SQL migration files
      ↓
    mysqlmig
      ↓
  mysql client
      ↓
    MySQL
```

DBAs write SQL migration files, commit them to Git, and use `mysqlmig` to execute them against a MySQL database.

`mysqlmig` is intentionally MySQL-specific and DBA-oriented. It does not attempt to become a general-purpose migration framework. DBAs remain responsible for writing correct SQL and understanding the resulting database state.

Implementation requirements:

* Python 3.12 or newer (the floor is Ubuntu 24.04's system `python3`)
* standard library only
* single script (`mysqlmig.py`)
* uses the existing `mysql` command-line client
* no Python MySQL driver
* no `mysqlsh`
* no installation/package setup required

---

## 2. Repository Layout

```text
mysqlmig/
├── mysqlmig.py
├── README.md
├── DESIGN.md
├── LICENSE
└── migrations/
```

Multiple databases can be managed from the same repository by using separate migration directories.

For example:

```text
migrations/
├── application/
│   ├── v1.0_initial.sql
│   ├── v1.1_add_users.sql
│   └── v1.2_add_indexes.sql
└── reporting/
    ├── v1.0_initial.sql
    └── v1.1_add_reports.sql
```

---

## 3. Migration Files

Migration files use:

```text
{VERSION}_{DESCRIPTION}.sql
```

Examples:

```text
v1.0_initial.sql
v1.1_add_users.sql
v1.2.3_add_indexes.sql
v1.10.20_cleanup.sql
1.0_initial.sql
1.1_add_users.sql
```

The version contains one or more numeric components separated by dots.

The `v` prefix is optional.

The description may contain underscores, hyphens, and other characters suitable for a filename. The parser must clearly distinguish the version from the description.

Only files matching this format are considered migrations. Other files in the migration directory are ignored.

For example:

```text
migrations/
├── v1.0_initial.sql
├── v1.1_add_users.sql
├── README.md
├── notes.txt
└── rollback/
```

Only the `.sql` files matching the migration filename format are processed. `mysqlmig` does not execute SQL files that cannot be identified as migrations.

### Encoding

Migration files must be UTF-8 encoded text files.

`mysqlmig` does not perform encoding detection or automatic character-set conversion. A migration file that cannot be read as UTF-8 is considered invalid and must not be executed.

### Version Ordering

Versions are compared numerically by component, not lexically:

```text
v1.9 < v1.10
```

The version stored in `schema_version.version` is exactly the version written in the filename. For example `v1.10` is stored as `v1.10` and is not normalized to another representation.

The migration filename identifies the migration script, while the version determines execution order and migration identity.

Two migration files that resolve to the same version are invalid, regardless of their descriptions or prefix.

For example:

```text
v1.10_add_index.sql
1.10_cleanup.sql
```

cannot both exist in the same migration directory because both represent version `1.10`.

Migration gaps are allowed. For example `v1.0`, `v1.2`, `v1.5` is valid. Duplicate versions are not.

### Version Prefix

The first migration establishes the prefix convention for the directory.

If the first migration uses `v`, all migrations must use `v`.

If the first migration does not use `v`, subsequent migrations must not use it.

Mixing the two styles within the same migration directory is invalid.

### Modified Historical Migrations

`mysqlmig` does not use checksums or hashes to detect modified migration files.

The design assumes that migrations already applied to a database are not modified. Changing an already-applied migration file is considered a repository/process error.

If a historical migration needs to be changed, the correct approach is to create a new migration. `mysqlmig` relies on the migration version and filename recorded in `schema_version` and does not provide automatic repair or reconciliation of modified historical migrations.

---

## 4. Migration State

`mysqlmig` stores migration state in the target database.

### schema_version

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    id              INT UNSIGNED   NOT NULL AUTO_INCREMENT,
    version         VARCHAR(32)    NOT NULL,
    script_name     VARCHAR(255)   NOT NULL,
    status             ENUM('IN_PROGRESS', 'SUCCESS', 'FAILED') NOT NULL,
    execution_start    TIMESTAMP(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    execution_duration VARCHAR(16)    NULL,
    execution_log      LONGTEXT       NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_schema_version_version (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

Column semantics:

* `id` — execution-history record identifier. It represents the order in which migration records were created. It does **not** define migration ordering; migration ordering is always determined from the parsed migration versions.
* `version` — migration identity and version. Unique.
* `script_name` — migration filename, for example `v1.2_add_indexes.sql`.
* `status` — `IN_PROGRESS`, `SUCCESS`, or `FAILED`.
* `execution_start` — millisecond-precision time the latest execution attempt started. It is populated by the database default (`CURRENT_TIMESTAMP(3)`) when the initial record is inserted; `mysqlmig` does not supply a value. On retry, it is reset to the start of the new attempt.
* `execution_duration` — human-readable `HH:MM:SS.mmm` duration of the latest completed execution attempt (millisecond precision, matching the `TIMESTAMP(3)` columns), for example `00:00:12.345`. It is computed by the database on completion from the difference between `execution_start` and `NOW(3)` using native time functions (`SEC_TO_TIME` / `TIME_FORMAT`), and is `NULL` while a migration is `IN_PROGRESS`.
* `execution_log` — complete captured output from the MySQL client. It is `NULL` while a migration is `IN_PROGRESS`.

The table is created as `utf8mb4` so that captured MySQL client output stored in `execution_log` round-trips correctly.

### Record Lifecycle

Each migration record is written in two steps, each performed through its own short-lived `mysql` client connection to the target database:

1. **On start** — `mysqlmig` inserts the record with `status = IN_PROGRESS`. `execution_start` is set by the database default, and `execution_duration` and `execution_log` are left `NULL`.
2. **On completion** — once the migration finishes, `mysqlmig` updates the same record to `status = SUCCESS` or `status = FAILED`, sets `execution_log`, and sets `execution_duration` from the server-side difference between `execution_start` and `NOW(3)`, formatted as a human-readable duration.

Computing the duration in the database keeps a single clock source (the MySQL server) for both the start time and the elapsed time, avoiding any mix of client and server clocks.

This makes an in-flight migration visible in `schema_version` and leaves an inspectable record if `mysqlmig` is interrupted before the migration completes.

An `IN_PROGRESS` record is the mechanism that prevents a second migration run against the same database (see Concurrency Protection). Because the last record must be `SUCCESS` before any new migration can start, a concurrent or subsequent `run` that finds an `IN_PROGRESS` record stops and treats it as a blocking migration. An `IN_PROGRESS` record is therefore either a currently running migration or a leftover from an interrupted one; in both cases a new migration is correctly prevented from starting.

The `id` column provides a natural history order:

```sql
SELECT *
FROM schema_version
ORDER BY id;
```

The most recent migration record can be obtained with:

```sql
SELECT *
FROM schema_version
ORDER BY id DESC
LIMIT 1;
```

### Current Version

The current schema version is the latest migration with `status = SUCCESS`.

A blocking migration (`FAILED` or `IN_PROGRESS`) does not advance the current version.

Example:

```text
id  version  status
1   v1.0     SUCCESS
2   v1.1     SUCCESS
3   v1.2     FAILED
```

Current successful version:

```text
v1.1
```

Blocking migration:

```text
v1.2
```

A blocking migration therefore represents a pending migration that has already been attempted but has not completed successfully.

### Blocking Migration State

A migration record whose `status` is not `SUCCESS` is a blocking state. There are two such states:

* `FAILED` — the migration ran and the `mysql` client reported an error. This includes the case where the server aborts the running statement (for example, `KILL QUERY` from another session): the client returns a non-zero status, so the migration is recorded `FAILED`.
* `IN_PROGRESS` — the migration was started but `mysqlmig` was interrupted before it could record a final result (for example, `mysqlmig` itself was killed with Ctrl-C, or the connection was lost). The record is left `IN_PROGRESS` because no final status was ever written.

The distinction is intentional: a server-aborted query is a completed-with-error execution (`FAILED`), whereas killing `mysqlmig` mid-run leaves the outcome unknown (`IN_PROGRESS`).

Failed migrations are recorded in `schema_version` intentionally: the history table should let a DBA see that a migration was attempted without inspecting log files. An `IN_PROGRESS` record left behind by an interrupted run serves the same purpose for migrations that may never have completed.

There may be at most one non-`SUCCESS` record (`FAILED` or `IN_PROGRESS`), and it must always be the latest migration record. No later migration may execute until that migration succeeds.

Therefore the following state is invalid:

```text
id  version  status
1   v1.0     SUCCESS
2   v1.1     FAILED
3   v1.2     SUCCESS
```

When a blocking migration is retried successfully, the existing row is updated:

```text
FAILED      → SUCCESS
IN_PROGRESS → SUCCESS
```

The existing `id` is preserved because it represents the migration's position in execution history. A new row is not inserted; the unique `version` constraint guarantees there cannot be two records for the same migration.

On retry start, the existing row is reset to `IN_PROGRESS`, `execution_start` is updated to the new attempt, and `execution_duration` is cleared. On completion, the row is updated with the final status and `execution_duration`. The new output is appended to `execution_log` rather than replacing the previous attempt:

```text
--- FAILED ATTEMPT ---
ERROR 1060 (42S21): Duplicate column name 'status'

--- RETRY ---
Query OK, 0 rows affected
```

If the retry fails again, the row becomes `FAILED` and the new output is appended.

### Blocking Migration Retry

When `run` encounters a blocking migration, it must not silently execute it again. The migration may have partially modified the database — this is true both for a `FAILED` migration and for an `IN_PROGRESS` migration that was interrupted at an unknown point.

Interactive execution must warn the DBA and ask for explicit confirmation before retrying.

The warning names the record's actual status.

For a failed migration:

```text
Migration v1.2_cleanup.sql is in state FAILED.

The migration may have partially changed the database.

Retry this migration? [y/N]:
```

For an interrupted (in-progress) migration:

```text
Migration v1.2_cleanup.sql is in state IN_PROGRESS.
It was interrupted and may not have finished.

The migration may have partially changed the database.

Retry this migration? [y/N]:
```

In non-interactive mode the same condition is an error (naming the status, e.g.
"Migration v1.2_cleanup.sql is in state FAILED.") and execution stops.

If the DBA does not confirm, execution stops.

`mysqlmig` does not attempt to determine whether the database is safe to retry. The DBA is responsible for inspecting and resolving any partial changes.

---

## 5. Migration Execution

Each migration file is executed using a separate `mysql` client process. This is preferred over opening one long-lived session and using `SOURCE`, because it provides clear execution boundaries, simple error handling, independent timing, complete output capture, immediate history recording, and easy identification of the migration that failed.

The migration file is supplied through standard input:

```text
mysql [fixed options] [configured/CLI options] < migration.sql
```

Migration SQL is not passed as command-line arguments. This allows migration files to contain multiline statements, stored program definitions, comments, and MySQL client `delimiter` commands.

The MySQL client is always invoked with:

```text
-vvv
--comments
```

`-vvv` ensures detailed client output is available for capture.

`--comments` ensures SQL comments are preserved when statements are sent to MySQL, including comments inside stored procedures, functions, triggers, and other stored program definitions.

`mysqlmig` does not add `--force`, because a SQL error must terminate the current migration and cause it to be recorded as `FAILED`.

The complete MySQL client output is captured regardless of whether `--verbose` was requested.

After the `mysql` process completes, `mysqlmig` checks its exit status. A zero exit status indicates success; a non-zero exit status indicates failure.

### Transactions

`mysqlmig` does not wrap migrations in transactions.

Migration SQL controls its own transaction behavior. This is necessary because MySQL DDL may implicitly commit and many schema changes cannot be safely treated as a single transactional operation.

If a migration partially modifies the database and then fails, `mysqlmig` does not attempt to roll back those changes. Partial changes are the DBA's responsibility.

### Migration Result

Each migration is recorded in two steps:

1. **Before execution** — insert an `IN_PROGRESS` record for the migration. `execution_start` is set by the database default; `execution_duration` and `execution_log` are left `NULL`.
2. **After execution** — capture the complete output and update the record to its final status. The database computes `execution_duration` during the update as the difference between `execution_start` and `NOW(3)`.

On success, the record is updated to `SUCCESS` with the computed `execution_duration` and captured `execution_log`.

On failure:

1. the record is updated to `FAILED` with `execution_duration` and `execution_log`
2. execution stops
3. later migrations are not executed

The insert, the migration SQL, and the update are each run through a separate `mysql` client connection to the target database. `mysqlmig` does not hold a persistent connection open across the run.

If migration SQL succeeds but updating the record fails, `mysqlmig` reports the inconsistency and does not automatically rerun the migration. In this situation the record remains `IN_PROGRESS`, which the next `run` treats as a blocking migration requiring confirmation.

---

## 6. Execution Logging

The complete output from every MySQL client execution is stored in `schema_version.execution_log`.

`execution_log` is `NULL` while a migration is `IN_PROGRESS` and is populated when the migration completes.

This includes successful and failed executions and provides a persistent record of:

* MySQL output
* MySQL errors
* SQL echoed by the client under `-vvv`
* other useful client messages

On retry, new output is appended to the existing log rather than replacing the original failed output, preserving the history of both attempts in a single record.

The log is retained in the database so that migration failures can be investigated after the original `mysqlmig` process has ended.

The log may contain a large amount of text and is intended primarily for troubleshooting. It is therefore excluded from normal history inspection through a database view (see below).

Execution logs contain raw MySQL client output and should be treated as potentially sensitive. `mysqlmig` must not intentionally print passwords or other sensitive connection information to terminal output or migration history.

---

## 7. Database View

`mysqlmig` creates the following view together with the metadata table:

```sql
CREATE OR REPLACE VIEW v_schema_version AS
SELECT
    id,
    version,
    script_name,
    status,
    execution_start,
    execution_duration
FROM schema_version;
```

The view intentionally excludes `execution_log`. The duration column is named
`execution_duration` in both the table and the view; the `history` command's
`DURATION` column reflects the same value.

The normal history query is therefore:

```sql
SELECT *
FROM v_schema_version
ORDER BY id;
```

The full log can be queried directly from `schema_version` when troubleshooting:

```sql
SELECT
    version,
    script_name,
    status,
    execution_start,
    execution_duration,
    execution_log
FROM schema_version
WHERE version = 'v1.7';
```

Keeping the log in `schema_version` avoids introducing a separate log table.

---

## 8. Commands

The command structure is:

```text
mysqlmig [OPTIONS] [COMMAND] [OPTIONS]
```

Options may appear before or after the command, in any order — for example
`mysqlmig -D app run` and `mysqlmig run -D app` are equivalent. The
restriction is that `--to` is valid only with `status` and `run`, and
`--verbose` is valid only with `run` or with interactive mode (no command, so
a DBA can enable verbose output for interactive runs). Using `--to` with
another command or with no command, or `--verbose` with `init`/`status`/
`history`, is an error.

Commands:

```text
init
history
status
run
```

### init

Creates the metadata objects (`schema_version` and `v_schema_version`) in the
target database.

```bash
mysqlmig init
```

`init` does **not** create the database. The target database must already exist
and be accessible to the connecting user — in production, migrations typically
run as a dedicated user that does not hold the `CREATE DATABASE` privilege, so
database provisioning is left to the DBA. If the database is missing or
inaccessible, the `mysql` client reports the error (e.g. `Unknown database`) and
`init` stops with a non-zero exit.

Table and view creation uses `CREATE TABLE/VIEW IF NOT EXISTS`, so `init` is
idempotent. Running it on an already-initialized database succeeds (exit `0`)
and reports a warning that the objects are already present. (This is detected
from the server's "table already exists" note, surfaced via the client's
`--show-warnings`, rather than a prior existence check.)

### history

Displays migration history using `v_schema_version`.

```bash
mysqlmig history
```

This is a read-only operation.

### status

Displays the current migration state, including:

* current successful version
* latest available version
* target version
* pending migrations
* blocking migration (`FAILED` or `IN_PROGRESS`), if any

```bash
mysqlmig status
```

The target defaults to `latest`. A specific target can be supplied with `--to`
to preview the plan up to that version without executing anything:

```bash
mysqlmig status --to v1.7
```

With a specific target, the pending list is limited to the migrations that
would run to reach that target, and the reported target version reflects it.

This is a read-only operation.

### run

Executes pending migrations up to the requested target.

```bash
mysqlmig run
```

The default target is `latest`.

A specific target can be supplied:

```bash
mysqlmig run --to v1.7
```

Verbose output can be requested:

```bash
mysqlmig run --verbose
```

To preview what a run would do without executing anything, use `status`, which
reports the current version, target, and the pending migrations. Passing the
same `--to VERSION` to `status` previews the plan for that specific target.

### Pending Migrations

Pending migrations are migration files with versions greater than the current successful version and up to the requested target. They are executed in numeric version order.

```text
Current: v1.4
Target:  v1.7

Pending:
  v1.5_add_index.sql
  v1.6_new_table.sql
  v1.7_cleanup.sql
```

If a blocking migration exists (`FAILED` or `IN_PROGRESS`), it must be resolved first; no later migration is considered executable until it succeeds.

### No Pending Migrations

If the current version already matches the target, `run` prints the full status
(identical to the `status` command) rather than a partial summary, so the
output is consistent regardless of which command produced it:

```text
Database: application
 Current: v1.7
  Target: v1.7
  Latest: v1.7

No pending migrations.
```

This is a successful execution state and returns exit code `0`.

### Metadata Initialization

`mysqlmig` does not silently create its metadata as a side effect of read-only commands.

If `schema_version` does not exist in the target database:

* `history` and `status` report that the database is not initialized and exit. The target audience is a DBA, so the report is a plain statement of fact:

  ```text
  schema_version does not exist in database 'application'.
  The database has not been initialized by mysqlmig.
  ```

* `run` creates `schema_version` and `v_schema_version` as part of starting the first migration:
  * In interactive mode, `mysqlmig` asks for confirmation before creating the metadata, then shows the available migrations:

    ```text
    schema_version does not exist in database 'application'.
    Create it now? [y/N]:
    ```

  * In non-interactive mode, `run` creates the metadata without prompting, because running `run` is itself the explicit instruction to apply migrations.

The metadata objects are created with `CREATE TABLE IF NOT EXISTS` / `CREATE OR REPLACE VIEW`, so creation is idempotent.

### Help and Version

`--help` and `--version` are handled before any command dispatch or database access.

Non-interactive (invoked with the option on the command line):

```bash
mysqlmig --help
mysqlmig --version
```

Each prints its text to stdout and exits with code `0`. `--help` prints usage, the available commands, and the supported options; `--version` prints the tool name and version.

Interactive:

* The version is always shown in the interactive header.
* The menu offers a help option. Choosing it prints the help text and then redisplays the menu.

---

## 9. Interactive and Non-Interactive Mode

Execution mode is determined by the presence of a valid command.

Running `mysqlmig` without a command starts interactive mode. Running it with a valid command (`status`, `history`, `run`) runs non-interactively.

TTY state does not determine the mode. It only affects presentation details such as whether color output is used.

### Interactive

When no command is supplied, running:

```bash
mysqlmig
```

opens an interactive menu.

Example:

```text
MySQL Migration Tool v0.2.0

Connected to:
            Host: db-prod.example.com
            User: migrator@%
        Database: application
  Migrations dir: migrations/application

What would you like to do?

  1. Initialize database
  2. Status
  3. History
  4. Run migrations
  5. Help
  6. Exit

Select:
```

Before the menu is shown, interactive mode validates two things, failing fast
if either is wrong:

1. the migration directory — `mysqlmig` checks it exists and is well-formed
   (so a bad `--dir` is reported immediately, not only when the first action
   runs); and
2. the connection — a single `SELECT CURRENT_USER()`. If it fails (bad
   host/user/password, or an inaccessible database), the error is printed and
   mysqlmig exits with a non-zero status instead of showing a menu, matching
   the `mysql` client's "connect on launch" behavior.

On success, the header groups the connection details under a plain `Connected
to:` label, with the labels right-aligned so their colons line up under
`Migrations dir:` (the widest): the host (the configured value, not the
server's `@@hostname`), the authenticated user from `CURRENT_USER()`, the
database, and the migration directory (`Migrations dir`). Seeing these with no
error confirms the connection is good, the database exists, and the directory
is valid, so the DBA can proceed.

The menu provides access to the same `init`, `status`, `history`, and `run`
operations available through explicit commands; the entries are colored to
match the meaning used elsewhere in the tool. Choosing Help prints the help
text and then redisplays the menu.

Interactive `run` may ask for confirmation before executing migrations and before retrying a blocking migration (`FAILED` or `IN_PROGRESS`).

Output uses color where useful. In the status block, each version value is bold in its own color so the three are easy to tell apart: **Current** bold yellow, **Target** bold light blue, **Latest** bold green; the database name is bold white, pending migrations are shown in the script color. On a successful run, `Migration complete.` is bold green and the final `Current version:` value is bold yellow (matching Current). Color is a presentation detail only: it is disabled automatically when stdout is not a TTY, independent of execution mode.

### Non-Interactive

When a valid command is supplied, `mysqlmig` runs non-interactively:

```bash
mysqlmig status
mysqlmig history
mysqlmig run
```

Non-interactive execution never prompts. This allows the same commands to be used from scripts, CI systems, cron jobs, and other automated environments.

If `run` encounters a blocking migration (`FAILED` or `IN_PROGRESS`) that requires confirmation, it stops with an error:

```text
ERROR: Migration v1.7_cleanup.sql previously failed.
The migration may have partially changed the database.
An interactive terminal is required to confirm the retry.
```

Because a command selects non-interactive mode, a command-driven `run` that would otherwise require confirmation stops with an error rather than prompting.

---

## 10. Previewing a Run

`mysqlmig` does not have a separate dry-run flag. The `status` command already
shows the current version, the target, and the pending migrations without
changing anything, so it serves as the preview of what a `run` would do.

```bash
mysqlmig status
```

Example:

```text
Database: application
 Current: v1.4
  Target: v1.7
  Latest: v1.7

Pending migrations:
  v1.5_add_index.sql
  v1.6_new_table.sql
  v1.7_cleanup.sql
```

The labels are right-aligned so their colons line up under `Database:`, and the
version values are shown in bold.

By default the target is `latest`. To preview a run that stops at a specific
version, pass the same `--to VERSION` to `status`:

```bash
mysqlmig status --to v1.6
```

```text
Database: application
 Current: v1.4
  Target: v1.6
  Latest: v1.7

Pending migrations:
  v1.5_add_index.sql
  v1.6_new_table.sql
```

The reported `Target` reflects the requested version and the pending list is
limited to the migrations that would run to reach it. Being read-only,
`status` executes no migration SQL and creates or modifies no history records.

---

## 11. Verbose Output

The MySQL client always runs with `-vvv`. This is independent of the `mysqlmig` verbosity option, and the full captured output is always stored in `schema_version.execution_log`.

`mysqlmig --verbose` / `-v` controls only what is displayed to the user. The
per-migration output is the same whether `run` is invoked non-interactively or
from the interactive menu, and the bold `Executing: <file>` header is shown in
both the plain and verbose forms — verbose only adds the separator rule and the
captured client output.

Without verbose output:

```text
Executing: v1.5_add_index.sql
  SUCCESS

Executing: v1.6_add_column.sql
  SUCCESS
```

With `--verbose`, each migration's captured client output is shown as a clearly
delimited block: the same bold `Executing: <file>` header, followed by a
separator rule and then the output, so successive scripts are easy to tell
apart.

```text
Executing: v1.5_add_index.sql
------------------------------------------------------------
--------------
ALTER TABLE orders ADD INDEX idx_status (status)
--------------

Query OK, 0 rows affected (2.31 sec)
Records: 0  Duplicates: 0  Warnings: 0

------------------------------------------------------------
  SUCCESS
```

The full output is captured in both cases. `--verbose` affects display only; it does not change SQL execution, MySQL client verbosity, or the contents of `execution_log`.

---

## 12. Configuration

`mysqlmig` follows normal MySQL option-file behavior rather than introducing its own configuration-file search path. A dedicated configuration file can be supplied when needed.

It supports:

```text
--defaults-file=FILE
--defaults-extra-file=FILE
```

`--defaults-file` specifies the option file to use instead of the normal MySQL option-file set.

`--defaults-extra-file` adds an additional option file to the normal option-file processing.

Both options are applied to a single source of truth: `mysqlmig` uses them to locate its own `[mysqlmig]` group **and** passes them through to every `mysql` client invocation so the client's connection settings (`[client]`, SSL, charset, and so on) are read from the same files. A DBA therefore controls where all configuration comes from with one flag.

The replace/augment semantics are the same for both `mysqlmig` and the `mysql` client:

* `--defaults-file` replaces the normal option-file set.
* `--defaults-extra-file` adds to the normal option-file set.

Example:

```bash
mysqlmig --defaults-file=/etc/mysqlmig.cnf status
```

Here `/etc/mysqlmig.cnf` is the only option file consulted — by `mysqlmig` for `[mysqlmig]` and by the `mysql` client for connection settings. If a setting the DBA relies on normally lives in `~/.my.cnf`, it must be present in this file too, because `--defaults-file` suppresses the default set.

A note on `--defaults-extra-file` precedence: this follows standard MySQL
client behavior. The extra file is read *before* the standard files (such as
`~/.my.cnf`), and when the same option appears in more than one file the
later-read file wins. So a value in `~/.my.cnf` overrides the same value in a
`--defaults-extra-file`. `--defaults-extra-file` therefore adds settings that
aren't otherwise present rather than overriding the standard files; use
`--defaults-file` when you need a single authoritative source. `mysqlmig` does
not change this ordering — it only forwards the flags to the client.

### [mysqlmig]

`mysqlmig` uses its own option group:

```ini
[mysqlmig]
db=application
dir=migrations
login-path=mysqlmig
host=db-server-01.example.com
port=3306
user=migrator
```

Supported settings:

* `db` — default target database
* `dir` — migration directory
* `login-path` — MySQL login path
* `host` — MySQL host
* `port` — MySQL port
* `user` — MySQL user
* `password` — MySQL password (last resort; see Password Handling below)

Only these keys are consulted from `[mysqlmig]`. Any other key found in the
group (for example a `mysql`-client option such as `connect-timeout`) is
**ignored with a warning on stderr**, naming the unsupported key(s) and the
supported set. This is deliberate: `[mysqlmig]` carries only mysqlmig's own
options, while general `mysql` client options belong in `[client]` (which the
client reads directly). The warning prevents a DBA from silently expecting an
unsupported setting to take effect. It is a warning, not an error — the command
still runs.

The connection settings (`host`, `port`, `user`, `login-path`) let a DBA keep
migration-specific credentials in `[mysqlmig]`, separate from a general-purpose
`[client]` group. For each of these, the effective value is resolved as:

```text
command-line option  >  [mysqlmig]  >  the mysql client's own option files
```

So `[mysqlmig] user=migrator` overrides `[client] user=...`, and a CLI
`--user` overrides both. `host`/`port`/`user` are passed through to the `mysql`
client as command-line options; `password` is handled specially (see below).

Migration files live under a top-level `migrations/` folder, with one subfolder per database. The migration directory is resolved as follows:

* An explicit `--dir` (or `[mysqlmig] dir`) is used literally, regardless of the database name. For example `--db demo --dir /opt/m/demo` uses `/opt/m/demo` exactly.
* Otherwise, if the database was given explicitly on the command line (`--db`), the directory is `migrations/<database>`. For example `--db demo` uses `migrations/demo`.
* Otherwise (the database came from the `[mysqlmig]` default, with no `--db` on the command line), the directory is the base `migrations/` folder itself, with no subfolder.

Command-line values take precedence over `[mysqlmig]`.

Credentials are not stored or managed by `mysqlmig`.

### Login Path and Host

A login path provides MySQL authentication information. It should not be used to define a server-specific target host when the same credentials are used across multiple servers.

The target host is independent and may come from normal MySQL configuration:

```ini
[client]
host=db-server-01.example.com
```

or from:

```bash
mysqlmig status --host=db-server-01.example.com
```

This allows the same login path and credentials to be used against different MySQL endpoints:

```text
Server 1 → login-path=mysqlmig + host=db-server-01
Server 2 → login-path=mysqlmig + host=db-server-02
Server 3 → login-path=mysqlmig + host=db-server-03
```

The credentials remain managed by MySQL's login-path mechanism.

### Password Handling

The preferred ways to authenticate remain the MySQL client's own mechanisms —
a login path (`.mylogin.cnf`) or a `[client]`/`[mysqlmig]` option file — because
they keep the password out of mysqlmig entirely. However, a login path requires
`mysql_config_editor`, which is not always available (e.g. some Percona builds,
or air-gapped hosts where installing tools is impractical). For those cases
mysqlmig supports these additional ways to supply the password, following the
`mysql` client's own `-p`/`--password` conventions:

* **`-p` / `--password`** (bare, no value) — prompt for the password once,
  interactively, with no echo. This is the recommended option when a login
  path is unavailable: nothing is written to disk and nothing is stored.
* **`--password=VALUE` / `-pVALUE`** (attached value) — use `VALUE` directly.
  Convenient for scripts and parity with the `mysql` client. A space-separated
  `-p VALUE` is **not** treated as the password (again matching the client).
* **`password=` in `[mysqlmig]`** — a plaintext password in the option file.
  This is a **last resort**; a plaintext password at rest is inherently
  insecure and should only be used when no better mechanism is available, with
  the file protected by restrictive permissions (e.g. `chmod 600`).

Password precedence (within mysqlmig):

```text
-p / --password (prompt or value)  >  [mysqlmig] password
```

**How the password reaches the client.** When mysqlmig has an explicit password
(from any of the above), it passes it to the `mysql` client as a
`--password=VALUE` command-line argument. This is deliberate and matches the
`mysql` client's own precedence rules:

* A command-line `--password=` **overrides** a password set in an option file
  (such as `~/.my.cnf [client] password=`). This is the behavior a DBA expects:
  if you supply a password to mysqlmig, it must take effect.
* `MYSQL_PWD` (the environment variable) was considered but **rejected**: in the
  `mysql` client's precedence, an option-file password beats `MYSQL_PWD`, so a
  password relayed that way would be silently ignored whenever `~/.my.cnf`
  contains one — giving the wrong result without any error.

The tradeoff is the standard one: a `--password=` argument is visible in the
process list (`ps`) for the lifetime of each `mysql` child, exactly as
`mysql --password=...` is. mysqlmig does not try to hide this; a DBA supplying a
password on the command line is making the same choice as with the `mysql`
client. To avoid it entirely, use a login path or let the client's own option
files authenticate — in which case mysqlmig passes **no** password argument at
all and the client's normal mechanisms apply unchanged.

When no password is supplied to mysqlmig, nothing is appended, so authentication
is left completely to the `mysql` client (login path, `[client]`/`[mysqlmig]`
option files, etc.).

Note: `my_print_defaults` masks password values, so mysqlmig reads the
`[mysqlmig]` `password` directly from the option file (with `configparser`)
rather than through that tool.

### Configuration Precedence

```text
Command line
     ↓
[mysqlmig] configuration
     ↓
Built-in defaults
```

MySQL connection and authentication behavior is delegated to the MySQL client wherever possible. mysqlmig does not implement its own credential storage; the only password it ever handles is one supplied via `-p`/`--password` or `[mysqlmig] password`, which it passes to the client as a `--password=` argument as described above.

---

## 13. MySQL Client Option Handling

`mysqlmig` delegates MySQL connection and authentication to the existing `mysql` client. Options are divided into three categories.

### 13.1 mysqlmig Options

These options are interpreted by `mysqlmig` itself and are not passed to `mysql`:

```text
--help
--version
--db (-D)
--dir
```

The following are command-scoped options, also interpreted by `mysqlmig`:

```text
--to (-t)        valid with status and run
--verbose (-v)   valid with run, and with interactive mode (no command)
```

`--to` is valid with the `status` and `run` commands. `--verbose` is valid with the `run` command and with interactive mode (no command), so verbose output can be enabled for interactive runs; it is not valid with `init`, `status`, or `history`. Supplying an option with a command it does not apply to, or `--to` with no command, is a command-line usage error (mysqlmig reports it rather than falling into interactive mode).

The `--db` option is interpreted by `mysqlmig` because the database name is part of migration state and is used when determining the default migration directory. It is aligned with the MySQL client's `-D` database option, and `mysqlmig` passes the corresponding database option through to each `mysql` invocation.

### 13.1a Option-File Options (dual-purpose)

The option-file options are the exception to the three-category split: they are both interpreted by `mysqlmig` and passed through to `mysql`.

```text
--defaults-file=FILE
--defaults-extra-file=FILE
```

`mysqlmig` uses them to locate its `[mysqlmig]` group and also forwards them to every `mysql` invocation so the client reads its connection settings from the same files. See the Configuration section for the replace/augment semantics.

When present, these options must be forwarded as the **first** arguments on the `mysql` command line, before any connection or fixed options, because the `mysql` client requires `--defaults-file` / `--defaults-extra-file` to precede all other options.

### 13.2 MySQL Connection Options

The following MySQL connection options are supported by `mysqlmig` and passed through to every `mysql` client invocation:

```text
--host HOST (-h)
--port PORT (-P)
--user USER (-u)
--login-path NAME (-L)
```

The password is also a connection option. When supplied it is passed to the
client as `--password=VALUE` (see Password Handling for why, and the `ps`
tradeoff):

```text
--password[=VALUE] (-p)   Bare: prompt (no echo). =VALUE / -pVALUE: use VALUE.
```

The database is supplied using:

```text
--db DATABASE (-D)
```

`mysqlmig` translates this into the corresponding MySQL client database option when invoking `mysql`.

Examples:

```bash
mysqlmig run -h db-prod.example.com -P 3306 -u migrator -D application
```

results in the migration `mysql` process receiving the corresponding connection options.

```bash
mysqlmig run -L production
```

causes the migration `mysql` process to use `--login-path=production`.

`mysqlmig` does not need to reproduce every available MySQL client option. The objective is to support the connection options normally required by DBAs while delegating authentication and connection handling to the MySQL client.

### 13.3 Fixed MySQL Client Options

`mysqlmig` always adds the following options to every migration `mysql` invocation:

```text
-vvv
--comments
```

`-vvv` ensures that detailed MySQL client output is available for capture and storage in `schema_version.execution_log`.

`--comments` ensures that comments are preserved when SQL is sent to the server, which is particularly important for stored procedures, functions, triggers, and other stored program definitions.

These options are controlled by `mysqlmig` and cannot be disabled through normal configuration. `mysqlmig` must not add `--force`; a SQL error must terminate the current migration.

No other MySQL client behavior should be changed unnecessarily. Connection, authentication, SSL, timeout, character-set, and other client settings remain configurable through normal MySQL option files or supported command-line options.

### 13.4 Configuration and Option Precedence

MySQL connection options may originate from normal MySQL option files or from the `mysqlmig` command line.

For example:

```ini
[client]
host=db-prod.example.com
port=3306
```

can provide the default connection information. A command-line option supplied to `mysqlmig` overrides the corresponding configured value:

```bash
mysqlmig run -h db-test.example.com
```

overrides the host configured through `[client]`. The resulting values are passed to the `mysql` client when the migration is executed.

`mysqlmig` uses the MySQL client's normal option-file processing and adds the `mysqlmig`-specific configuration layer described in the configuration section.

### 13.5 Migration Execution

Each migration is executed through a separate `mysql` client process. Conceptually, the invocation is:

```text
mysql [--defaults-file / --defaults-extra-file, if supplied]
      [mysqlmig connection options]
      [fixed mysqlmig options]
      < migration.sql
```

If `--defaults-file` or `--defaults-extra-file` was supplied to `mysqlmig`, it is placed first on the `mysql` command line, as required by the client. The remaining option-file configuration (`[client]`, and `[mysqlmig]`-derived connection settings) is read by the client from those files.

The exact command-line construction is an implementation detail, but the resulting `mysql` process must receive the option-file flags (first), the required connection options, and the fixed options described above.

The short-lived `mysql` connections used by `mysqlmig` for metadata and state operations (reading `schema_version`, inserting and updating records, creating metadata) are separate from these migration `mysql` processes.

---

## 14. Database and Target Selection

The target database is selected from:

1. command-line option (`--db` / `-D`)
2. `[mysqlmig]` configuration
3. supported default behavior

An explicit command-line value takes precedence over the configuration value. The database name is also used as the default migration directory when no `dir` is configured. `--db` / `-D` applies to all commands.

The target migration version is supplied with `--to`, valid for both `status` and `run`:

```text
mysqlmig run --to VERSION
mysqlmig run -t VERSION
mysqlmig status --to VERSION
```

or:

```text
mysqlmig run --to latest
mysqlmig run -t latest
```

`latest` means the highest available migration version, and is the default when `--to` is omitted. `--to` is valid with `run` (where it bounds execution) and `status` (where it previews the plan up to that target); it has no meaning for `init` or `history`.

The requested target must exist in the migration directory. A target lower than the current successful version is invalid. For `run`, an invalid or missing target causes `mysqlmig` to fail before executing migrations; for `status`, the same validation applies before the preview is produced.

---

## 15. Target Validation and Repository Consistency

Before executing migrations, `mysqlmig` validates:

* migration filenames
* version syntax
* version-prefix convention
* duplicate versions
* the requested target version
* repository/database consistency
* blocking migration state (`FAILED` or `IN_PROGRESS`)

### Repository/Database Consistency

If the database contains a successful migration that does not exist in the repository, execution fails.

If the database is ahead of the repository, execution fails.

`mysqlmig` does not attempt to repair these situations automatically.

---

## 16. Concurrency Protection

`mysqlmig` prevents two migration runs from operating on the same target database at the same time using the `IN_PROGRESS` record in `schema_version`, not an external lock.

The rule is simple: a `run` may start a migration only when the latest `schema_version` record is `SUCCESS` (or when there are no records yet). Before executing a migration, `mysqlmig` inserts an `IN_PROGRESS` record; after it completes, the record becomes `SUCCESS` or `FAILED`.

This gives mutual exclusion for free:

* While a migration is running, the latest record is `IN_PROGRESS`, so a second `run` against the same database sees a blocking migration and stops rather than starting a new migration.
* The unique constraint on `version` prevents two records for the same migration, so two runs cannot both insert an `IN_PROGRESS` record for the same version — one of the inserts fails.

Because each database has its own `schema_version` table, migrations against different databases are naturally independent and can run concurrently.

A `run` that finds an existing `IN_PROGRESS` record reports it as a blocking migration:

```text
Migration v1.2_cleanup.sql is already in progress or did not finish.
```

and stops. In interactive mode it may offer to retry it (see Blocking Migration Retry); in non-interactive mode it stops with an error.

### No Persistent Connection or Advisory Lock

`mysqlmig` does not open a long-lived MySQL session and does not use `GET_LOCK`. Every database interaction — inserting and updating `schema_version` records, reading migration state, and creating metadata — is performed through a separate short-lived `mysql` client connection to the target database. Migration SQL is likewise executed through its own `mysql` client process.

This keeps the implementation simple and avoids session-scoped lock lifetime concerns, while the `IN_PROGRESS` record provides the required serialization.

### Read-Only Commands

`history` and `status` are read-only operations. They never insert an `IN_PROGRESS` record and are unaffected by a migration currently in progress; they simply report the current state, including any `IN_PROGRESS` record.

---

## 17. Migration Directory

Migrations are organized under a top-level `migrations/` folder, with one
subfolder per database. The migration directory is resolved as:

1. If `--dir` or `[mysqlmig] dir` is set, use it literally (full-path override).
2. Else if `--db` was given on the command line, use `migrations/<db>`.
3. Else (the database came from the `[mysqlmig]` default), use the bare
   `migrations/` folder, with no subfolder.

Examples:

```text
--db demo                       -> migrations/demo
--db demo --dir /opt/m/demo     -> /opt/m/demo
(db from [mysqlmig], no --db)   -> migrations
```

If the resulting directory does not exist, `mysqlmig` stops with an error:

```text
ERROR: Migration directory 'migrations/demo' does not exist.
```

`mysqlmig` does not create the directory automatically.

---

## 18. Execution Output

Normal `run` output is concise.

Success:

```text
Executing: v1.5_add_index.sql
  SUCCESS

Executing: v1.6_add_column.sql
  SUCCESS

Migration complete.

Current version: v1.6
```

`Migration complete.` is bold green; a blank line separates it from the final
`Current version:` line, whose value is bold yellow (matching Current in
`status`).

Failure:

```text
Executing: v1.7_cleanup.sql
  FAILED

Migration failed.
Current version remains: v1.6
Blocking migration: v1.7_cleanup.sql (FAILED)
```

If `mysqlmig` is interrupted mid-migration, the record is left `IN_PROGRESS` and the next run reports it the same way:

```text
Blocking migration: v1.7_cleanup.sql (IN_PROGRESS)
```

On a later run that encounters a blocking migration interactively, the retry prompt reflects the record's status.

For a failed migration:

```text
Previous migration failed:

  v1.7_cleanup.sql

The migration may have partially changed the database.

Retry this migration? [y/N]:
```

For an interrupted (in-progress) migration:

```text
Previous migration did not finish:

  v1.7_cleanup.sql

It is unknown how much of the migration was applied.

Retry this migration? [y/N]:
```

With `--verbose`, the full output captured from each MySQL client process is displayed.

`history` displays the concise migration history (the large execution log is not displayed):

```text
ID  VERSION  SCRIPT                STATUS   EXECUTION START      DURATION
1   v1.0     v1.0_initial.sql      SUCCESS  2026-10-07 18:00:01  00:00:02.100
2   v1.1     v1.1_add_users.sql    SUCCESS  2026-10-07 18:00:04  00:00:01.800
3   v1.2     v1.2_add_indexes.sql  FAILED   2026-10-07 18:00:08  00:00:03.700
```

`execution_start` is shown as the start time. A row that is still `IN_PROGRESS` (for example after an interrupted run) has no `execution_duration` yet, which is rendered as a blank or `-`:

```text
ID  VERSION  SCRIPT                STATUS       EXECUTION START      DURATION
3   v1.2     v1.2_add_indexes.sql  IN_PROGRESS  2026-10-07 18:00:08  -
```

History column widths are sized to the data and adapt to the terminal width.
The `SCRIPT` column (filenames can be long) is truncated with a trailing `...`
when there is not enough room, so the table always stays aligned.

`status` provides a higher-level summary intended for quickly determining whether the database requires migration.

---

## 19. Error Handling

`mysqlmig` must fail clearly and predictably, and must not attempt automatic repair.

The main failure rules are:

* missing `mysql` executable → stop
* invalid command-line usage → stop
* missing/invalid migration directory → stop
* invalid migration filename or version syntax → stop
* mixed `v` prefix conventions → stop
* duplicate version → stop
* invalid or missing target → stop
* database ahead of repository → stop
* migration history containing a migration missing from the repository → stop
* failed migration during execution → update record to `FAILED` and stop
* existing blocking migration (`FAILED` or `IN_PROGRESS`) → retry only after interactive confirmation
* non-interactive retry of a blocking migration → stop
* concurrent `run` → stop
* metadata operation failure → stop
* successful migration with failed record update → report inconsistency; leave record `IN_PROGRESS`; do not rerun automatically

A migration failure stops execution immediately. No subsequent migration is executed after a failure.

The tool should report useful human-readable context without unnecessarily reinterpreting MySQL errors. The complete MySQL client output remains available in `execution_log`. No automatic repair or conflict resolution is performed.

---

## 20. Exit Codes

`mysqlmig` uses these exit codes:

```text
0    Success
1    Error
130  Interrupted (Ctrl-C / SIGINT)
```

Exit code `0` is returned for successful read-only commands and successful migration runs, including when there are no pending migrations.

Exit code `1` is returned for any error, including invalid usage, invalid migration state, a failed migration, a blocking migration that cannot be retried non-interactively, and metadata or connection failures. The specific cause is described in a human-readable message on stderr rather than encoded in the exit code.

Exit code `130` is returned when `mysqlmig` is interrupted with Ctrl-C (`SIGINT`), for example during a long-running migration. Rather than dumping a Python traceback, `mysqlmig` catches the interrupt, prints a short `Interrupted. Exiting.` warning on stderr, and exits with `130` (the conventional shell code for SIGINT). If a migration was mid-flight, its `schema_version` record is left `IN_PROGRESS`; `status` will show it as blocking, and an interactive `run` can retry it.

---

## 21. Security

`mysqlmig` does not implement its own password storage. Authentication is delegated to the MySQL client and mechanisms such as `.my.cnf`, `.mylogin.cnf`, and `--login-path`.

The recommended mechanism for dedicated migration credentials is a MySQL login path:

```bash
mysql_config_editor set \
    --login-path=mysqlmig \
    --host=db.example.com \
    --user=mysqlmig \
    --password
```

`mysqlmig` can then use `--login-path=mysqlmig`.

The same `.mylogin.cnf` can be deployed to multiple servers when the credentials are the same. If the MySQL endpoint differs between servers, the host should be configured independently through `[client]` or `--host`.

`mysqlmig` should avoid displaying passwords or sensitive connection information. Execution logs contain raw MySQL client output and should be treated as potentially sensitive.

DBAs remain responsible for protecting credential files and migration repositories.

---

## 22. Migration SQL

Migration SQL is ordinary MySQL SQL. `mysqlmig` does not introduce a migration DSL.

```sql
ALTER TABLE users
    ADD COLUMN status VARCHAR(20) NOT NULL;
```

Stored procedures, functions, triggers, events, and other MySQL objects can be defined using normal MySQL client syntax. Migration files may use MySQL client `delimiter` commands where required.

`mysqlmig` does not parse or rewrite SQL. The MySQL client is responsible for interpreting and executing it. The DBA is responsible for ensuring that the SQL is safe to execute against the target database.

---

## 23. Rollback

`mysqlmig` does not provide automatic rollback.

A rollback is another explicit migration:

```text
v1.5_add_column.sql
v1.6_add_index.sql
v1.7_remove_index.sql
```

`mysqlmig` does not automatically reverse a failed migration. The DBA is responsible for deciding whether a rollback is safe and for writing the appropriate corrective SQL.

---

## 24. Non-Goals

`mysqlmig` deliberately does not provide:

* automatic rollback
* migration checksums or modification detection
* dependency graphs
* migration DSLs
* ORM integration
* schema diffing
* automatic repair
* conflict resolution
* branching migration histories
* automatic handling of partially completed migrations
* custom credential storage
* database drivers
* `mysqlsh`
* application deployment
* unnecessary abstraction layers

The tool should remain small and predictable.

---

## 25. Internal Architecture

The implementation remains a single Python script using only the standard library. The code should be logically separated into small functions without unnecessary abstraction.

Conceptually:

```text
mysqlmig
│
├── CLI / configuration
│
├── migration discovery
│
├── version validation
│
├── metadata / state access (short-lived mysql connections)
│      ├── metadata queries
│      ├── history/status queries
│      └── schema_version insert/update
│
└── migration execution
       └── separate mysql process per migration
```

Expected logical components:

```text
main()
parse_arguments()
load_configuration()
discover_migrations()
validate_migrations()
load_schema_version()
calculate_target()
calculate_pending()
interactive_confirmation()
insert_in_progress_record()
execute_migration()
finalize_migration_record()
```

Each metadata operation and each migration runs through its own short-lived `mysql` client connection. There is no persistent service connection.

---

## 26. Overall Execution Flow

### init

```text
mysqlmig init
      │
      ├── load configuration
      ├── connect to MySQL (database must already exist; else error + exit 1)
      ├── create schema_version + v_schema_version   (IF NOT EXISTS, idempotent)
      └── report initialized / already initialized (via --show-warnings)
```

### history

```text
mysqlmig history
      │
      ├── load configuration
      ├── connect to MySQL
      ├── if metadata missing → report "not initialized" and exit
      ├── SELECT from v_schema_version
      └── display result
```

### status

```text
mysqlmig status
      │
      ├── load configuration
      ├── connect to MySQL
      ├── if metadata missing → report "not initialized" and exit
      ├── inspect migration state
      ├── inspect migration files
      ├── calculate current/target/pending state
      └── display result
```

### run

```text
mysqlmig run
      │
      ├── load configuration
      ├── check metadata exists (create only if confirmed; see below)
      ├── inspect migration state
      ├── validate target and repository consistency
      ├── retry blocking migration (FAILED or IN_PROGRESS) if confirmed
      └── execute pending migrations
            ├── insert IN_PROGRESS record (new mysql connection)
            ├── run migration SQL (separate mysql process)
            ├── capture output
            └── update record to SUCCESS/FAILED, DB computes execution_duration
                  (new mysql connection; stop on failure)
```

---

## 27. Example Usage

Interactive:

```bash
mysqlmig
```

History:

```bash
mysqlmig history
```

Status:

```bash
mysqlmig status
```

Run latest migrations:

```bash
mysqlmig run
```

Run to a specific version:

```bash
mysqlmig run --to v1.7
```

Initialize the database and metadata:

```bash
mysqlmig init
```

Verbose:

```bash
mysqlmig run --verbose
```

Specify database:

```bash
mysqlmig run -D application
```

Specify host:

```bash
mysqlmig run -h db-prod.example.com
```

Specify login path:

```bash
mysqlmig run -L production
```

Combine options:

```bash
mysqlmig run \
    -h db-prod.example.com \
    -P 3306 \
    -D application \
    -L production \
    --to v1.7
```

Use dedicated configuration:

```bash
mysqlmig --defaults-file=/etc/mysqlmig.cnf run
```

---

## 28. Example Migration Repository

```text
migrations/
├── v1.0_initial.sql
├── v1.1_add_users.sql
├── v1.2_add_orders.sql
├── v1.3_add_order_indexes.sql
├── v1.4_add_status.sql
└── README.md
```

Example migration:

```sql
-- v1.2_add_orders.sql

CREATE TABLE orders (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id BIGINT UNSIGNED NOT NULL,
    status VARCHAR(20) NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id)
);
```

Possible database history:

```text
id  version  status
1   v1.0     SUCCESS
2   v1.1     SUCCESS
3   v1.2     SUCCESS
4   v1.3     FAILED
```

In this state:

```text
Current successful version: v1.2
Blocking migration:         v1.3 (FAILED)
Pending migration:          v1.4
```

`v1.4` cannot execute until `v1.3` is successfully retried. After a successful retry, execution can proceed with `v1.4`.

If instead `mysqlmig` had been interrupted while applying `v1.3`, the row would be left as `IN_PROGRESS` and would block execution in the same way until it is retried to `SUCCESS`.

Execute with:

```bash
mysqlmig run
```

---

## 29. Implementation Phases

### Phase 1 — Core

* migration discovery
* version parsing/comparison
* migration validation
* `schema_version`
* `v_schema_version`
* MySQL client execution
* execution logging
* success/failure recording
* `history`
* `status`
* `run`

### Phase 2 — Safety

* blocking migration state (`FAILED` and `IN_PROGRESS`)
* interactive retry confirmation
* IN_PROGRESS-based concurrency protection
* verbose output

### Phase 3 — Configuration and CLI

* MySQL option files
* `[mysqlmig]`
* login paths
* connection options
* interactive menu
* non-interactive commands
* exit codes

The phases may be implemented incrementally, but the final behavior must conform to this design.

---

## 30. Acceptance Criteria

The implementation is complete when:

### Migration Files

* Valid migration files are discovered.
* Non-migration files are ignored.
* Versions are compared numerically.
* Duplicate versions are rejected.
* The version-prefix convention is enforced.
* UTF-8 migration files are supported.

### State

* `schema_version` is created automatically.
* `v_schema_version` is created automatically.
* An `IN_PROGRESS` record is inserted before a migration runs, then updated to the final status on completion.
* `execution_start` is populated by the database default on insert.
* `execution_duration` and `execution_log` are `NULL` while a migration is `IN_PROGRESS`.
* Successful migrations are recorded as `SUCCESS`.
* Failed migrations are recorded as `FAILED`.
* An interrupted migration is left as `IN_PROGRESS` and is visible in history.
* Blocking migrations (`FAILED` or `IN_PROGRESS`) block later migrations.
* Only one non-`SUCCESS` record can exist, and it is always the latest migration record.
* The current successful version never advances because of a blocking migration.
* Blocking migrations can be retried interactively.
* Successful retries update the existing record instead of creating a duplicate.
* Retry output is appended to the log.

### Execution

* Each migration uses a separate `mysql` process.
* `mysql` always receives `-vvv`.
* `mysql` always receives `--comments`.
* `mysql` never receives `--force`.
* Full MySQL output is captured in `execution_log`.
* `-v` / `--verbose` controls display only.
* Migrations are not automatically wrapped in a transaction.
* A SQL error stops execution.

### Commands

* `history` displays concise history.
* `status` displays migration state.
* `run` executes migrations.
* Interactive mode provides a menu.
* Non-interactive mode requires an explicit command.
* `init` creates the database (if needed) and the metadata, idempotently.
* `run --to VERSION` supports a specific target.
* `status` previews pending migrations without executing anything.

### Concurrency

* A migration starts only when the latest `schema_version` record is `SUCCESS` (or there are no records).
* A second `run` against the same database while a migration is `IN_PROGRESS` stops rather than starting a new migration.
* The unique `version` constraint prevents two runs from recording the same migration.
* Different databases can run concurrently.
* `history` and `status` are unaffected by a migration in progress.

### Configuration

* MySQL option files are supported.
* `[mysqlmig]` is supported.
* `--defaults-file` and `--defaults-extra-file` are both read by `mysqlmig` and passed through to the `mysql` client.
* The option-file flags are forwarded first on the `mysql` command line.
* Login paths are supported.
* Host can be configured independently from the login path.
* CLI options override `[mysqlmig]`.

### Error Handling

* Missing `mysql` executable is reported.
* Missing migration directories are errors.
* Invalid targets are rejected.
* Database-ahead-of-repository and missing historical migrations are rejected.
* Migration failures exit with code `1`.
* Metadata failures are reported.
* A leftover `IN_PROGRESS` record from an interrupted run blocks later migrations and requires confirmation to retry.
* Successful migrations are not automatically rerun after a record-update failure; the record is left `IN_PROGRESS`.
* Non-interactive execution never prompts.

### Supported CLI Options

Global:

```text
--help
--version
--db DATABASE (-D)
--dir DIRECTORY
--host HOST (-h)
--port PORT (-P)
--user USER (-u)
--login-path NAME (-L)
--password[=VALUE] (-p)
--defaults-file=FILE
--defaults-extra-file=FILE
```

`run`:

```text
--to VERSION|latest (-t)
--verbose (-v)
```

### Implementation

* The implementation remains a single Python script using only the standard library.
* MySQL credentials are not managed by `mysqlmig`.

---

## 31. Guiding Philosophy

`mysqlmig` should remain a small DBA tool, not a database deployment framework.

It automates the repetitive and deterministic parts:

* finding migrations
* ordering migrations
* executing SQL
* tracking migration history
* capturing execution output
* detecting failures and making them visible
* preventing unsafe concurrent execution
* showing status and history

The DBA remains responsible for:

* writing migration SQL
* reviewing migrations
* understanding MySQL behavior
* handling partial changes after a failed or interrupted migration
* confirming that a blocking migration is safe to retry
* deciding when corrective or rollback migrations are appropriate
* maintaining migration files in Git

The intended architecture remains:

```text
Git
 │
 │ migration SQL
 ▼
mysqlmig
 │
 ├── metadata/state ──────► MySQL
 │
 ├── history/status ──────► MySQL
 │
 └── migration SQL ───────► mysql client ─────► MySQL
```

The guiding principle is:

> Automate execution and history, not database decisions.
>
> Keep the tool small, deterministic, and transparent.

`mysqlmig` should make the existing DBA migration process safer and more deterministic without hiding database behavior from the DBA.
