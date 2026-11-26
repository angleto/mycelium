"""Test-only defaults and async-engine isolation.

CI/explicit env always wins (setdefault). The app keeps a process-wide
async engine; pytest-asyncio uses one event loop per test, so we
dispose and reset the engine after each test (engine-per-loop in
tests). Production pooling is unchanged.
"""

from __future__ import annotations

import os

os.environ.setdefault("MYCELIUM_JWT_SECRET", "test-only-secret-min-32-bytes-aaaaaaaaaa")
# Valid Fernet key (urlsafe-b64 of 32 zero bytes); test-only.
os.environ.setdefault("MYCELIUM_SECRET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
# Dedicated pepper for the issuer-API-key keyed hash (>=32 chars); test-only.
os.environ.setdefault("MYCELIUM_ISSUER_KEY_PEPPER", "test-only-issuer-key-pepper-aaaaaaaaaaaa")
# The suite has no proxy chain to anchor and drives the issuer through
# X-Forwarded-Host, so it runs the open OAuth strategies through their explicit
# opt-out rather than with MYCELIUM_PUBLIC_BASE_URL / trusted proxies (which
# would change what other suites assert). Tests of the gate itself set these
# variables explicitly (api/tests/test_oauth_open_endpoints_gate.py).
os.environ.setdefault("MYCELIUM_OAUTH_UNATTRIBUTED_ORIGINS_OK", "true")

import pathlib
from collections.abc import AsyncIterator, Iterator

import pytest

import mycelium_core.db as _db
from mycelium_core.attachment_store import set_attachment_store_override
from mycelium_core.services.mailer import LogMailer, set_mailer

_HARDEN_ACLS_SQL = pathlib.Path(__file__).parent / "deploy" / "local" / "harden_function_acls.sql"
_MARK_TEST_DB_SQL = pathlib.Path(__file__).parent / "deploy" / "local" / "mark_test_database.sql"


# One database per pytest-xdist worker.
#
# The suite assumes one process per database, and several tests rest on it:
# a fixture purges every S3-form attachment after each test, the admin user
# list is asserted over every user, the migration tests step the schema down
# and back up and lift row level security from every table, and the
# function ACL script is applied by each process at start. With several
# workers on one database each of those broke another worker's tests
# (measured on 2026-11-26: 2298 errors and failures at -n 8, all from these
# four). Rather than teach each one about its neighbours, every worker gets
# the situation they were written for: a database of its own. A serial run
# (no xdist) is unchanged and uses the database it was given.
#
# The xdist controller, which configures before any worker starts, migrates
# one template database from nothing to head and prepares it like the
# shared one (the test-database marker; the function ACLs are applied by the
# session fixture below, in every process, as before). Each worker clones
# it with CREATE DATABASE ... TEMPLATE, which copies files and runs no
# migration: eight workers migrating at once exhausted the server's lock
# table ("out of shared memory"), and a copy is also much faster. The
# controller drops the template at the end, each worker its own copy.
#
# The database the suite was pointed at must already be a test database:
# the guard runs on it before anything is created next to it, so the suite
# never creates databases on a server nobody declared disposable.
def _sibling_url(url: str, suffix: str) -> str:
    from sqlalchemy.engine import make_url

    u = make_url(url)
    return u.set(database=f"{u.database}_{suffix}"[:63]).render_as_string(hide_password=False)


def _on_server(sync_url: str, *statements: str) -> None:
    import sqlalchemy as sa

    # CREATE / DROP DATABASE cannot run inside a transaction nor inside the
    # database they name; the maintenance database serves for both.
    admin = sa.create_engine(
        sa.engine.make_url(sync_url).set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as conn:
            for statement in statements:
                conn.exec_driver_sql(statement)
    finally:
        admin.dispose()


def _db_name(url: str) -> str:
    from sqlalchemy.engine import make_url

    name = make_url(url).database
    if not name:
        raise pytest.UsageError(f"the test database URL names no database: {url!r}")
    return name


def pytest_configure(config: pytest.Config) -> None:
    sync_url = os.environ.get("MYCELIUM_DATABASE_URL_SYNC")
    async_url = os.environ.get("MYCELIUM_DATABASE_URL")
    if not sync_url or not async_url:
        return
    worker = getattr(config, "workerinput", {}).get("workerid")
    template = _db_name(_sibling_url(sync_url, "template"))
    if worker is None:
        if not getattr(config.option, "numprocesses", None):
            return  # a serial run: the database it was given
        import sqlalchemy as sa
        from _test_db_guard import assert_test_database
        from alembic import command
        from alembic.config import Config

        assert_test_database(sync_url=sync_url, async_url=async_url)
        _on_server(
            sync_url,
            f'DROP DATABASE IF EXISTS "{template}" WITH (FORCE)',
            f'CREATE DATABASE "{template}"',
        )
        config.stash[_OWN_DB] = (sync_url, template)
        template_url = _sibling_url(sync_url, "template")
        os.environ["MYCELIUM_DATABASE_URL_SYNC"] = template_url
        try:
            command.upgrade(
                Config(str(pathlib.Path(__file__).parent / "core" / "alembic.ini")), "head"
            )
        finally:
            os.environ["MYCELIUM_DATABASE_URL_SYNC"] = sync_url
        engine = sa.create_engine(template_url)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(_MARK_TEST_DB_SQL.read_text())
        finally:
            engine.dispose()
        return
    own_sync = _sibling_url(sync_url, worker)
    own = _db_name(own_sync)
    _on_server(
        sync_url,
        f'DROP DATABASE IF EXISTS "{own}" WITH (FORCE)',
        f'CREATE DATABASE "{own}" TEMPLATE "{template}"',
    )
    config.stash[_OWN_DB] = (sync_url, own)
    os.environ["MYCELIUM_DATABASE_URL_SYNC"] = own_sync
    os.environ["MYCELIUM_DATABASE_URL"] = _sibling_url(async_url, worker)
    from mycelium_core.config import get_settings

    get_settings.cache_clear()


def pytest_unconfigure(config: pytest.Config) -> None:
    held = config.stash.get(_OWN_DB, None)
    if held is not None:
        shared_sync, name = held
        _on_server(shared_sync, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


# The database this process created, and the URL of the server to drop it on.
_OWN_DB = pytest.StashKey[tuple[str, str]]()


@pytest.fixture(scope="session", autouse=True)
def _refuse_a_database_that_is_not_for_tests() -> None:
    """Stop before the first write if the target has not been declared
    disposable. See ``_test_db_guard`` for why the check is a marker table
    and not a name or an environment variable.

    Everything below that touches the database takes this fixture as an
    argument. Declaration order between two autouse fixtures of the same
    scope is not a contract worth resting a destructive operation on; a
    parameter is an edge in the dependency graph, and pytest honours it."""
    from _test_db_guard import assert_test_database

    from mycelium_core.config import get_settings

    settings = get_settings()
    assert_test_database(sync_url=settings.database_url_sync, async_url=settings.database_url)


@pytest.fixture(scope="session", autouse=True)
def _reproduce_prod_function_acls(_refuse_a_database_that_is_not_for_tests: None) -> None:
    """Make the test DB execute-posture match production (ADR-0015).

    Production grants the runtime role ``mycelium_app`` only what is
    explicitly granted: the default PUBLIC execute on functions is revoked
    out-of-band. A dev/CI DB built straight from the migrations keeps that
    default PUBLIC execute, which silently masks a missing
    ``GRANT EXECUTE ... TO mycelium_app`` -- a function the app calls
    directly then works in tests and 500s in prod (the
    /advisory/what-now -> ``tasks_event_end`` incident, migration 0059).

    Apply the shared ``harden_function_acls.sql`` once per session, as the
    OWNER (BYPASSRLS) role -- the same ``database_url_sync`` Alembic uses --
    via a plain sync libpq connection (mirrors ``_purge_s3_only_attachments``
    and sidesteps the async engine-per-loop teardown). Idempotent. The
    posture is asserted by ``test_function_acl_posture`` so a silent skip
    here cannot reopen the gap unnoticed. If no DB is reachable (e.g. an
    offline CLI-only run) this skips: DB-backed tests would fail on their
    own connection anyway."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import OperationalError

    from mycelium_core.config import get_settings

    engine = create_engine(get_settings().database_url_sync)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(_HARDEN_ACLS_SQL.read_text())
    except OperationalError:
        return
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
async def _dispose_engine() -> AsyncIterator[None]:
    yield
    engine = _db._engine
    if engine is not None:
        await engine.dispose()
        _db._engine = None
        _db._sessionmaker = None


@pytest.fixture(autouse=True)
def _reset_attachment_store_override() -> Iterator[None]:
    """Safety net: the attachment-store override is a process-global
    (like the engine above). A test that selects the s3/Fake backend
    must never leak it into the next test -- a leaked Fake backend makes
    later (default-pg) tests write S3-only rows (data NULL), which by
    design blocks the migration downgrade. Reset unconditionally after
    every test, independent of any per-fixture finally ordering."""
    yield
    set_attachment_store_override(None)


@pytest.fixture(autouse=True)
def _reset_mailer() -> Iterator[None]:
    """Safety net: the system mailer is a process-global (like the
    engine and the attachment-store override). Auth/W1b tests inject a
    capturing fake via ``set_mailer``; if an early assertion failure
    skipped their ``finally``, the fake would leak into later tests
    (a later auth flow would silently append to the wrong list / a
    stale fake). Reset to the default ``LogMailer`` unconditionally
    after every test, independent of per-test finally ordering. The
    real app/worker wire SMTP outside pytest (lifespan / worker main),
    so this never affects production."""
    yield
    set_mailer(LogMailer())


@pytest.fixture(autouse=True)
def _purge_s3_only_attachments() -> Iterator[None]:
    """DB-row safety net (the row analog of the override net above).

    The suite commits to a shared DB. An attachment row left in S3
    form (``data`` NULL, bytes off-DB) is illegal under the baseline
    schema's ``data NOT NULL`` invariant (pre-squash this was migration
    0049, now folded into 0001_baseline). S3-path tests clean inline,
    but that is skipped on an early assertion failure or for
    partially-created rows.

    Unconditionally purge S3-only rows after every test. RLS hides
    org-scoped rows from a no-tenant session (``admin_session`` is
    fail-closed by design), so this connects with the **owner**
    (BYPASSRLS) role -- the same ``database_url_sync`` Alembic and the
    migrator use -- via a plain sync libpq connection, which also
    sidesteps the async engine-per-loop teardown ordering. Real data
    is never NULL-``data`` unless deliberately migrated to S3; this
    only ever runs against the test DB."""
    yield
    import psycopg

    from mycelium_core.config import get_settings

    dsn = get_settings().database_url_sync.replace("+psycopg", "").replace("+asyncpg", "")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM attachments WHERE data IS NULL")
