# Changelog

All notable changes to `mysqlmig` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-10-11

First official release. `mysqlmig` is a single-file, standard-library-only
(Python 3.12+) MySQL schema migration executor for DBAs. It discovers versioned
SQL migration files, applies the pending ones in order, and records history in a
`schema_version` table, delegating all SQL execution to the system `mysql`
command-line client.

### Commands

- `init` — create the metadata objects (`schema_version` table and
  `v_schema_version` view) in an existing database. Idempotent; does not create
  the database itself.
- `status` — show current/target/latest versions, pending migrations, and any
  blocking migration. Read-only. Accepts `--to` to preview the plan up to a
  specific target.
- `history` — show the recorded migration history from `v_schema_version`.
- `run` — execute pending migrations up to the target (default: latest). Accepts
  `--to` and `--verbose`.
- Interactive menu when run with no command.

### Options

- Global: `--db`/`-D`, `--dir`, `--host`/`-h`, `--port`/`-P`, `--user`/`-u`,
  `--login-path`, `--password`/`-p` (`--password[=VALUE]`, bare `-p` prompts),
  `--defaults-file`, `--defaults-extra-file`, `--defaults-group-suffix`,
  `--help`, `--version`.
- `--to` applies to `status` and `run`; `--verbose` applies to `run` and to
  interactive mode.
- Options may appear before or after the command.
- Short options are kept consistent with the `mysql` client: `-h` is host (not
  help), and `--login-path` and `--to` have no short form (so they do not clash
  with the client's `-L`/`--skip-line-numbers` and `-t`/`--table`).

### Configuration and credentials

- Reads a `[mysqlmig]` option group (`db`, `dir`, `host`, `port`, `user`,
  `login-path`, `password`) via the MySQL option-file mechanism. Unsupported
  keys in the group are reported with a non-fatal warning.
- `--defaults-group-suffix=SUFFIX` additionally reads `[mysqlmig<SUFFIX>]`
  (overriding the base group) and is forwarded to the `mysql` client, enabling
  per-database option groups such as `[mysqlmig_db1]`.
- Precedence for connection settings: command line > `[mysqlmig]` > the mysql
  client's own option files.
- An explicitly supplied password is passed to the client as `--password=` so it
  correctly overrides an option-file password (matching the client; visible in
  `ps`, like `mysql --password=`). A blank `-p` prompt means an empty password,
  not a fallback to option files. A login path is preferred when available.

### Behavior

- Deterministic numeric version ordering (`v1.9` < `v1.10`); the `v` prefix is
  optional but the convention must be consistent within a directory.
- Each migration is run through its own short-lived `mysql` process; mysqlmig
  holds no persistent connection.
- Concurrency is serialized through an `IN_PROGRESS` record in `schema_version`,
  not an external lock.
- A failed or interrupted migration becomes a blocking record; a non-interactive
  run stops, while an interactive run can confirm a retry (which updates the
  record in place and appends to the execution log).
- No automatic rollback and no transaction wrapping — migration SQL controls its
  own transactions. There is no dry-run flag; `status` serves as the preview.
- Ctrl-C (SIGINT) exits cleanly with a short warning (no traceback) and exit
  code `130`.
- The `schema_version` table records `execution_start` and `execution_duration`
  (`HH:MM:SS.mmm`, computed by the database); the `v_schema_version` view omits
  the large `execution_log` column.

### Output

- Colorized output on a TTY (disabled when piped or when `NO_COLOR` is set).
- `status` version values are bold, each in its own color: Current cyan, Target
  green, Latest yellow.
- Interactive header groups connection details (host, authenticated user,
  database, migration directory) under a `Connected to:` label.
- `history` renders an aligned table with a `DURATION` column.

### Exit codes

- `0` success (including "no pending migrations"), `1` error, `130` interrupted
  (SIGINT).

[1.0.0]: https://keepachangelog.com/
