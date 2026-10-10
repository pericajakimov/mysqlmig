# mysqlmig

Simple, deterministic MySQL schema migration executor for DBAs.

`mysqlmig` runs SQL migration files against a MySQL database and tracks what has been
applied. It automates execution and history while leaving all SQL design and database
decisions to you. It is MySQL-specific, driver-free, and ships as a single Python script.

* Python, standard library only — no install step, no Python MySQL driver, no `mysqlsh`.
* Uses the existing `mysql` command-line client to run your SQL.
* Does not analyze, rewrite, or roll back SQL. Whatever is in a migration file is executed as-is.

For the full rationale and detailed behavior, see [DESIGN.md](DESIGN.md).

---

## Requirements

* Python 3.12 or newer (standard library only — no packages to install to run the tool).
* The `mysql` command-line client on `PATH`.
* A MySQL server you can connect to, with credentials available to the `mysql` client
  (for example via `.my.cnf`, `.mylogin.cnf`, or a login path).

---

## Migration files

Migrations live in a directory and are named:

```text
{VERSION}_{DESCRIPTION}.sql
```

Examples:

```text
v1.0_initial.sql
v1.1_add_users.sql
v1.2.3_add_indexes.sql
1.0_initial.sql
```

Rules:

* The version is one or more numeric components separated by dots. The `v` prefix is optional.
* The first migration in a directory sets the prefix convention; all later migrations must match it.
* Versions are ordered numerically (`v1.9` < `v1.10`), not alphabetically.
* Duplicate versions are not allowed. Gaps are fine.
* Files must be UTF-8. Files that do not match the format are ignored.

Migrations live under a top-level `migrations/` folder, one subfolder per
database. With `--db demo` and no `--dir`, mysqlmig looks in `migrations/demo`.
If the database comes from `[mysqlmig]` (no `--db`), it uses the bare
`migrations/` folder. `--dir` overrides the path entirely.

---

## Quick start

Run all pending migrations against a database:

```bash
mysqlmig run -D application
```

See the current state without changing anything:

```bash
mysqlmig status -D application
```

Start interactively (no command) and pick from a menu:

```bash
mysqlmig
```

---

## Commands

| Command   | Description                                              | Read-only |
|-----------|----------------------------------------------------------|-----------|
| `init`    | Create the metadata objects in an existing database      | no        |
| `status`  | Current version, latest, target, pending, and blockers   | yes       |
| `history` | Migration history from the `v_schema_version` view       | yes       |
| `run`     | Execute pending migrations up to the target              | no        |

### Examples

```bash
mysqlmig init                        # create the metadata in an existing db (idempotent)
mysqlmig status                      # current migration state (also previews pending)
mysqlmig history                     # applied-migration history
mysqlmig run                         # migrate to the latest version
mysqlmig run --to v1.7               # migrate up to and including v1.7
mysqlmig run --verbose               # show full mysql client output
```

To preview what a `run` would do without changing anything, use `status` — it
lists the pending migrations. (There is no separate dry-run flag.)

### Interactive vs non-interactive

* No command → **interactive** menu (header shows version/host/database; options: init,
  status, history, run, help).
* A command (`init` / `status` / `history` / `run`) → **non-interactive**; never prompts,
  suitable for scripts, CI, and cron.

---

## Options

Global options (valid for all commands):

```text
--help                 Show help and exit
--version              Show version and exit
--db DATABASE (-D)     Target database (also selects the default migration directory)
--dir DIRECTORY        Migration directory
--host HOST (-h)       MySQL host
--port PORT (-P)       MySQL port
--user USER (-u)       MySQL user
--login-path NAME (-L) MySQL login path
--defaults-file=FILE        Use only this option file (replaces the default set)
--defaults-extra-file=FILE  Add this option file to the default set
```

`run`-only options:

```text
--to VERSION|latest (-t)   Target version (default: latest)
--verbose (-v)             Display full mysql client output
```

Connection options (`--host`, `--port`, `--user`, `--login-path`) are passed through to the
`mysql` client. You can also provide them through normal MySQL option files.

---

## Configuration

`mysqlmig` uses MySQL option files and its own `[mysqlmig]` group:

```ini
[mysqlmig]
db=application
dir=migrations
login-path=mysqlmig
```

* `db` — default target database.
* `dir` — migration directory (literal path override; see directory resolution above).
* `login-path` — MySQL login path used for authentication.

Precedence: command line → `[mysqlmig]` configuration → built-in defaults.

`--defaults-file` and `--defaults-extra-file` control a single source of truth: `mysqlmig`
reads its `[mysqlmig]` group from them and also passes them to the `mysql` client, so the
client's connection settings come from the same files. `--defaults-file` uses only that file;
`--defaults-extra-file` adds to the normal set. Put anything the migration client needs
(`[client]` host, SSL, charset) in the same file when using `--defaults-file`.

```bash
mysqlmig --defaults-file=/etc/mysqlmig.cnf run
```

Credentials are never stored or managed by `mysqlmig`; authentication is delegated to the
`mysql` client. The recommended approach is a dedicated login path:

```bash
mysql_config_editor set --login-path=mysqlmig --host=db.example.com --user=mysqlmig --password
mysqlmig run -L mysqlmig -D application
```

The target host is independent of the login path, so the same credentials can be reused
across servers while pointing at different hosts via `[client]` or `--host`.

---

## How it tracks state

`mysqlmig` keeps history in a `schema_version` table in the target database, with a
companion `v_schema_version` view that omits the large log column for everyday queries.

Each migration is recorded in two steps:

1. An `IN_PROGRESS` record is inserted before the migration runs.
2. The record is updated to `SUCCESS` or `FAILED` when it finishes, with the duration
   (computed by the database) and the full `mysql` client output.

Everyday history view (log column excluded):

```sql
SELECT * FROM v_schema_version ORDER BY id;
```

Full log for one migration when troubleshooting:

```sql
SELECT version, script_name, status, execution_start, execution_time, execution_log
FROM schema_version
WHERE version = 'v1.7';
```

If `schema_version` does not exist yet, `status` and `history` report that the database is
not initialized and suggest running `mysqlmig init`. `init` creates the metadata objects in
an **existing** database (it does not create the database itself — provision that separately,
since migration users usually lack `CREATE DATABASE`); it is idempotent and reports a warning
if already initialized. `run` also creates the metadata as part of applying the first
migration (after confirmation in interactive mode), so running `init` first is optional.

---

## Failures, interruptions, and retries

* A migration is **not** wrapped in a transaction. Migration SQL controls its own transactions.
  A failed migration may have partially changed the database.
* A failed migration is recorded as `FAILED` and execution stops. It blocks all later migrations
  until it succeeds.
* If `mysqlmig` itself is interrupted mid-migration (e.g. Ctrl-C), the record is left
  `IN_PROGRESS` — the outcome is unknown. This also blocks later migrations.
* Note: if the server aborts the running statement instead (e.g. `KILL QUERY` from another
  session), the client returns an error, so the migration is recorded `FAILED` rather than
  `IN_PROGRESS`. (`FAILED` = ran and errored; `IN_PROGRESS` = mysqlmig was interrupted.)
* On the next `run`, a blocking migration (`FAILED` or `IN_PROGRESS`) must be retried first.
  Interactive mode asks for confirmation; non-interactive mode stops with an error naming the
  state (e.g. "Migration X is in state FAILED.").
* A successful retry updates the existing record in place (status → `SUCCESS`) and appends the
  new output to the log. No duplicate row is created.

`mysqlmig` does not decide whether the database is safe to retry — that is up to you.

### Concurrency

Two runs cannot migrate the same database at once: while a migration is `IN_PROGRESS`, any
other `run` sees a blocking record and stops. Different databases are independent and can run
concurrently. No external lock is used.

---

## Rollback

There is no automatic rollback. To reverse a change, write another migration:

```text
v1.5_add_column.sql
v1.7_remove_column.sql
```

---

## Exit codes

```text
0  Success (including "no pending migrations")
1  Error (cause described in the message)
```

---

## License

See [LICENSE](LICENSE).
