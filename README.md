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

## Installation

`mysqlmig` is a single self-contained script. Install it by copying the file onto
`PATH` as an executable named `mysqlmig` (drop the `.py`):

```bash
sudo install -m 0755 mysqlmig.py /usr/local/bin/mysqlmig
mysqlmig --version
```

`/usr/local/bin` is the conventional location for locally managed tools and is on
`PATH` by default; avoid `/usr/bin`, which belongs to the system package manager.
The script's shebang is `#!/usr/bin/python3` (the system interpreter on the
supported Ubuntu 24.04 / Python 3.12 floor); adjust it if your `python3` lives
elsewhere.

For a fleet, deploy with your configuration manager — for example an Ansible
`copy`/`template` task to `/usr/local/bin/mysqlmig` (mode `0755`, owner `root`),
followed by a `mysqlmig --version` smoke check.

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
mysqlmig status --to v1.7            # preview the plan up to and including v1.7
mysqlmig history                     # applied-migration history
mysqlmig run                         # migrate to the latest version
mysqlmig run --to v1.7               # migrate up to and including v1.7
mysqlmig run --verbose               # show full mysql client output
```

To preview what a `run` would do without changing anything, use `status` — it
lists the pending migrations. Pass `--to VERSION` to `status` to preview the
plan for a specific target rather than the latest. (There is no separate
dry-run flag.)

### Interactive vs non-interactive

* No command → **interactive** menu. On entry it validates the migration
  directory and the connection (`SELECT CURRENT_USER()`), failing fast with a
  clear error if the directory is bad or it can't connect; otherwise the header
  shows version and, under a `Connected to:` label, the host, authenticated
  user, database, and migration directory, then the menu (init, status,
  history, run, help). Pass `--verbose` to enable full mysql client output for
  interactive runs.
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
--login-path NAME      MySQL login path (no short form, like the mysql client)
--password[=VALUE] (-p) Bare -p prompts (no echo); --password=VALUE / -pVALUE uses VALUE
--defaults-file=FILE        Use only this option file (replaces the default set)
--defaults-extra-file=FILE  Add this option file to the default set
--defaults-group-suffix=SUFFIX  Also read [mysqlmig<SUFFIX>] (overrides [mysqlmig])
```

`status`/`run` options:

```text
--to VERSION|latest        Target version (default: latest).
                           With status, previews the plan up to that target;
                           with run, executes up to that target.
                           (No short form: -t is --table in the mysql client.)
--verbose (-v)             Display full mysql client output
                           (run, or interactive mode)
```

Options may appear **before or after the command**, in any order
(`mysqlmig -D app run` and `mysqlmig run -D app` are equivalent). `--to` is
valid with `status` and `run`; `--verbose` is valid with `run` and with
interactive mode (no command).

Connection options (`--host`, `--port`, `--user`, `--login-path`) are passed through to the
`mysql` client. You can also provide them through normal MySQL option files, or
in the `[mysqlmig]` group (see Configuration).

---

## Configuration

`mysqlmig` uses MySQL option files and its own `[mysqlmig]` group:

```ini
[mysqlmig]
db=application
dir=migrations
login-path=mysqlmig
host=db.example.com
port=3306
user=migrator
```

* `db` — default target database.
* `dir` — migration directory (literal path override; see directory resolution above).
* `login-path` — MySQL login path used for authentication.
* `host` / `port` / `user` — connection settings, so you can keep
  migration-specific credentials here, separate from a general `[client]` group.
* `password` — plaintext password (**last resort**; see Credentials below).

Only these keys are read from `[mysqlmig]`. Any other key in the group (for
example a mysql-client option like `connect-timeout`) is ignored with a warning
on stderr — put general mysql client options in `[client]`, which the client
reads directly.

Precedence for connection settings: command line → `[mysqlmig]` → the `mysql`
client's own option files (`[client]`, etc.). So `[mysqlmig] user=migrator`
overrides `[client] user=...`, and a CLI `--user` overrides both.

`--defaults-file` and `--defaults-extra-file` control a single source of truth: `mysqlmig`
reads its `[mysqlmig]` group from them and also passes them to the `mysql` client, so the
client's connection settings come from the same files. `--defaults-file` uses only that file;
`--defaults-extra-file` adds to the normal set. Put anything the migration client needs
(`[client]` host, SSL, charset) in the same file when using `--defaults-file`.

```bash
mysqlmig --defaults-file=/etc/mysqlmig.cnf run
```

The target host is independent of the login path, so the same credentials can be reused
across servers while pointing at different hosts via `[client]` or `--host`.

#### Per-database groups with `--defaults-group-suffix`

Mirroring the `mysql` client, `--defaults-group-suffix=SUFFIX` makes mysqlmig
also read `[mysqlmig<SUFFIX>]` in addition to `[mysqlmig]`, with the suffixed
group **overriding** the base. This is handy when you manage several databases
with different users/passwords: keep shared settings in `[mysqlmig]` and the
per-database differences in `[mysqlmig_db1]`, `[mysqlmig_db2]`, …

```ini
[mysqlmig]
host=db.example.com

[mysqlmig_db1]
db=db1
user=db1_migrator
password=...

[mysqlmig_db2]
db=db2
user=db2_migrator
password=...
```

```bash
mysqlmig --defaults-group-suffix=_db1 run   # uses db1 / db1_migrator
mysqlmig --defaults-group-suffix=_db2 run   # uses db2 / db2_migrator
```

The suffix is also forwarded to the `mysql` client, so a matching
`[client<SUFFIX>]` group (if you use one) applies to the connection too.

### Credentials

Authentication is best delegated to the `mysql` client. The recommended approach is a
dedicated login path:

```bash
mysql_config_editor set --login-path=mysqlmig --host=db.example.com --user=mysqlmig --password
mysqlmig run --login-path mysqlmig -D application
```

When a login path isn't available (e.g. `mysql_config_editor` isn't installed), you can
supply the password one of these ways:

* **Prompt (recommended):** bare `-p` / `--password` asks for the password once, with no
  echo. Nothing is written to disk.

  ```bash
  mysqlmig run -D application -u migrator -p
  ```

* **On the command line:** `--password=VALUE` or `-pVALUE` (attached, matching the `mysql`
  client; a space-separated `-p VALUE` is not treated as the password).

  ```bash
  mysqlmig run -D application -u migrator --password=secret
  ```

* **`password=` in `[mysqlmig]` (last resort):** a plaintext password in the option file.
  Only use this when nothing better is available, and protect the file (`chmod 600`).

Whenever you supply a password to mysqlmig (any of the above), it is passed to the `mysql`
client as a `--password=` argument. This is required so it correctly **overrides** a
password in an option file like `~/.my.cnf` — the `MYSQL_PWD` environment variable would
be silently ignored when such a file exists. The tradeoff is the standard one: like
`mysql --password=...`, the value is visible in `ps` for the `mysql` process's lifetime.
If you supply **no** password, mysqlmig passes none and the client authenticates on its
own (login path / option files).

Password precedence (within mysqlmig): `-p`/`--password` (prompt or value) →
`[mysqlmig] password`.

> Security note: a plaintext `password=` at rest, and a `--password=` on the command line,
> are both inherently insecure (readable in config files and in `ps` respectively). This
> is the same tradeoff as the `mysql` client itself. Prefer a login path whenever you can.

---

## How it tracks state

`mysqlmig` keeps history in a `schema_version` table in the target database, with a
companion `v_schema_version` view that omits the large log column for everyday queries
The duration column is named `execution_duration` in both the table and the view;
the `history` command shows the same value in its `DURATION` column.

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
SELECT version, script_name, status, execution_start, execution_duration, execution_log
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
* If `mysqlmig` itself is interrupted mid-migration (e.g. Ctrl-C), it exits cleanly with a
  short `Interrupted. Exiting.` warning (no traceback) and exit code `130`; the record is left
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
0    Success (including "no pending migrations")
1    Error (cause described in the message)
130  Interrupted with Ctrl-C (SIGINT)
```

---

## License

See [LICENSE](LICENSE).
