"""Shared pytest fixtures for mysqlmig tests.

Unit tests have no external dependencies.

Integration tests (marked with @pytest.mark.integration) require:
  * the `mysql` command-line client on PATH, and
  * a reachable MySQL server.

By default the tests connect with a bare `mysql` invocation, relying on the
developer's normal MySQL configuration (e.g. `~/.my.cnf`). No credentials are
hardcoded.

Connection details may optionally be overridden with environment variables so
the same suite runs in CI or against a non-default server:

  MYSQLMIG_TEST_LOGIN_PATH   use a MySQL login path, or
  MYSQLMIG_TEST_HOST         host
  MYSQLMIG_TEST_PORT         port
  MYSQLMIG_TEST_USER         user
  MYSQLMIG_TEST_PASSWORD     password
  MYSQLMIG_TEST_DB           throwaway database base name (default: mysqlmig_test)

If the server is not reachable, integration tests are skipped rather than failed,
so the unit suite still runs cleanly on machines without MySQL (e.g. CI).
"""

import os
import shutil
import subprocess
import uuid

import pytest


def _base_conn_args():
    """Build the mysql client connection args.

    With no environment overrides this returns an empty list, so the mysql
    client uses the developer's normal configuration (e.g. ~/.my.cnf).
    """
    login_path = os.environ.get("MYSQLMIG_TEST_LOGIN_PATH")
    if login_path:
        return ["--login-path=" + login_path]

    args = []
    host = os.environ.get("MYSQLMIG_TEST_HOST")
    if host:
        args.append("--host=" + host)
    port = os.environ.get("MYSQLMIG_TEST_PORT")
    if port:
        args.append("--port=" + port)
    user = os.environ.get("MYSQLMIG_TEST_USER")
    if user:
        args.append("--user=" + user)
    password = os.environ.get("MYSQLMIG_TEST_PASSWORD")
    if password:
        args.append("--password=" + password)
    return args


def _run_mysql(sql, database=None):
    """Run SQL through the mysql client. Returns CompletedProcess."""
    cmd = ["mysql", *_base_conn_args(), "--batch", "--skip-column-names"]
    if database:
        cmd.append(database)
    return subprocess.run(
        cmd,
        input=sql,
        capture_output=True,
        text=True,
    )


def _mysql_available():
    if shutil.which("mysql") is None:
        return False
    try:
        result = _run_mysql("SELECT 1;")
    except Exception:
        return False
    return result.returncode == 0


_MYSQL_OK = None


def mysql_available():
    """Cached availability check so we probe the server only once per run."""
    global _MYSQL_OK
    if _MYSQL_OK is None:
        _MYSQL_OK = _mysql_available()
    return _MYSQL_OK


@pytest.fixture()
def mysql_db():
    """Provide a freshly created, throwaway MySQL database, dropped on teardown.

    Skips the test if MySQL is not reachable.
    """
    if not mysql_available():
        pytest.skip("MySQL server not reachable; skipping integration test")

    base = os.environ.get("MYSQLMIG_TEST_DB", "mysqlmig_test")
    # Unique suffix so parallel/repeated runs never collide.
    dbname = f"{base}_{uuid.uuid4().hex[:8]}"

    create = _run_mysql(
        f"CREATE DATABASE `{dbname}` CHARACTER SET utf8mb4;"
    )
    if create.returncode != 0:
        pytest.skip("Could not create test database: " + create.stderr.strip())

    try:
        yield dbname
    finally:
        _run_mysql(f"DROP DATABASE IF EXISTS `{dbname}`;")


@pytest.fixture()
def mysql_conn_args():
    """The base connection args used to reach the test server."""
    return list(_base_conn_args())
