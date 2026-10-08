# Example migrations for trying out mysqlmig

This folder contains sample migrations for exercising the tool by hand. It is
for manual experimentation and is independent of the automated test suite.

```
migrations/
├── demo/            A clean, working migration sequence (v1.0 .. v1.3)
└── scenarios/       Extra files you copy into demo/ to trigger specific cases
```

## Setup

Create a throwaway database named `demo` (uses your normal MySQL config, e.g.
~/.my.cnf):

```bash
mysql -e "CREATE DATABASE demo CHARACTER SET utf8mb4;"
```

With `--db demo` and no `--dir`, mysqlmig looks in `migrations/demo`
automatically (each subfolder of `migrations/` is the set for a database of the
same name). A handy alias for the examples, run from the repo root:

```bash
alias mm='python3 mysqlmig.py -D demo'
```

(Adjust `python3`/path as you like; the tool is a single script.)

## 1. Happy path

```bash
mm status            # reports "not initialized" the first time
mm run               # creates metadata + applies v1.0 .. v1.3
mm status            # Current: v1.3, no pending
mm history           # one row per migration, SUCCESS, with timings
mm run               # "No pending migrations." (idempotent)
```

Try partial targets, dry-run, and verbose:

```bash
mysql -e "DROP DATABASE demo; CREATE DATABASE demo CHARACTER SET utf8mb4;"
mm run --to v1.1          # apply only v1.0 and v1.1
mm run --dry-run          # show the remaining plan, change nothing
mm run --verbose          # apply the rest, showing full mysql client output
```

## 2. Interactive menu

Run with no command to get the menu (version header, Status/History/Run/Help/Exit):

```bash
python3 mysqlmig.py -D demo
```

On a fresh database, choosing "Run" will prompt before creating the metadata.

## 3. Failure

Copy the failing migration into the demo sequence and run:

```bash
cp migrations/scenarios/v1.4_will_fail.sql migrations/demo/
mm run                    # v1.4 fails; recorded FAILED; execution stops
mm status                 # shows the blocking FAILED migration
mm run                    # non-interactive: refuses to retry, exits 1
mm history                # v1.4 row has status FAILED
```

Inspect the captured client output stored in the row:

```bash
mysql demo -e \
  "SELECT status, execution_log FROM schema_version WHERE version='v1.4'\G"
```

### Retry after fixing

Replace the broken file with the fixed one (same version) and retry
interactively:

```bash
cp migrations/scenarios/v1.4_fixed.sql migrations/demo/v1.4_will_fail.sql
python3 mysqlmig.py -D demo
# choose Run, then confirm the retry prompt
```

The v1.4 row flips FAILED -> SUCCESS in place and the retry output is appended
to the existing `execution_log` (check with the `\G` query above).

## 4. Interruption / hang (left IN_PROGRESS)

```bash
cp migrations/scenarios/v1.5_long_sleep.sql migrations/demo/
mm run                    # blocks inside SELECT SLEEP(60)
```

In another terminal, interrupt it (Ctrl-C the process, or kill the server-side
query via `SHOW PROCESSLIST` + `KILL QUERY <id>`). Then:

```bash
mm status                 # v1.5 is a blocking migration (IN_PROGRESS)
mm run                    # non-interactive: stops; interactive: offers retry
```

See `scenarios/v1.5_long_sleep.sql` for the full step-by-step.

## Cleanup

```bash
mysql -e "DROP DATABASE IF EXISTS demo;"
# remove any scenario files you copied into demo/
rm -f migrations/demo/v1.4_will_fail.sql migrations/demo/v1.5_long_sleep.sql
```
