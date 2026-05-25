"""Test-only defaults and async-engine isolation.

CI/explicit env always wins (setdefault). The app keeps a process-wide
async engine; pytest-asyncio uses one event loop per test, so we
dispose and reset the engine after each test (engine-per-loop in
tests). Production pooling is unchanged.
"""

from __future__ import annotations

import os

os.environ.setdefault("FLOW_JWT_SECRET", "test-only-secret-min-32-bytes-aaaaaaaaaa")
# Valid Fernet key (urlsafe-b64 of 32 zero bytes); test-only.
os.environ.setdefault("FLOW_SECRET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

from collections.abc import AsyncIterator, Iterator

import pytest

import flow_core.db as _db
from flow_core.attachment_store import set_attachment_store_override
from flow_core.services.mailer import LogMailer, set_mailer


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

    from flow_core.config import get_settings

    dsn = get_settings().database_url_sync.replace("+psycopg", "").replace("+asyncpg", "")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM attachments WHERE data IS NULL")
