"""Attachment storage seam: store selection, the Fake round-trip, and
the in-DB -> object-store data migrator (idempotent, no-op for pg).

No boto3, no network: the S3 path is exercised only through the Fake
injected via ``set_attachment_store_override`` (same pattern as the
LLM/embedder fakes). DB is the test PG (admin session: these are
infra rows, not tenant-scoped behaviour)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from _fake_attachment_store import FakeAttachmentStore
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import mycelium_core.db as _db
from mycelium_core.attachment_store import (
    PgAttachmentStore,
    S3AttachmentStore,
    get_attachment_store,
    set_attachment_store_override,
)
from mycelium_core.config import Settings, get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.migrate_attachments import migrate_attachments
from mycelium_core.models.attachment import Attachment

# The migrator is a deploy-side cross-tenant ops CLI: in production it
# runs with the owner role (BYPASSRLS), exactly like Alembic migrations
# (ADR-0015) and the dispatch worker -- a no-tenant ``admin_session``
# scan of ``organizations`` only returns rows under such a role
# (``test_rls.py`` pins the fail-closed 0-rows behaviour for the app
# role). This is that owner URL (the same one the alembic gate uses);
# derive it from the configured sync (owner) DSN so the test honours
# MYCELIUM_DATABASE_URL_SYNC (host/port/db) instead of a hardcoded
# localhost:5432, swapping only the driver to asyncpg for the async
# engine. The test drives the migrator under its real runtime contract.
_OWNER_DB_URL = get_settings().database_url_sync.replace("+psycopg", "+asyncpg")


def _s3_settings() -> Settings:
    return Settings(
        jwt_secret="x" * 40,
        secret_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        attachment_store="s3",
        attachment_s3_endpoint_url="https://s3.fr-par.scw.cloud",
        attachment_s3_region="fr-par",
        attachment_s3_bucket="mycelium-test",
        attachment_s3_access_key_id="ak",
        attachment_s3_secret_access_key="sk",
    )


def test_store_selection_by_config() -> None:
    pg = get_attachment_store(get_settings())  # default settings -> pg
    assert isinstance(pg, PgAttachmentStore)
    s3 = get_attachment_store(_s3_settings())
    assert isinstance(s3, S3AttachmentStore)


def test_s3_settings_fail_closed_when_incomplete() -> None:
    with pytest.raises(ValueError, match="attachment_store='s3' requires"):
        Settings(
            jwt_secret="x" * 40,
            secret_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            attachment_store="s3",
            # every S3 field missing
        )


def test_override_wins_over_config() -> None:
    fake = FakeAttachmentStore()
    set_attachment_store_override(lambda: fake)
    try:
        # Even with s3 selected, the override is returned (test seam).
        assert get_attachment_store(_s3_settings()) is fake
    finally:
        set_attachment_store_override(None)


async def test_fake_store_put_get_delete_round_trip() -> None:
    store = FakeAttachmentStore()
    await store.put("k1", b"hello-bytes", "text/plain")
    assert await store.get("k1") == b"hello-bytes"
    await store.delete("k1")
    with pytest.raises(KeyError):
        await store.get("k1")
    # delete is idempotent (missing key is a no-op).
    await store.delete("k1")


@pytest.fixture
async def _owner_engine() -> AsyncIterator[None]:
    """Point the global engine at the owner (BYPASSRLS) DB role for the
    duration of the test -- the migrator's real production runtime
    contract (deploy-side cross-tenant ops tool, like Alembic). The
    autouse ``_dispose_engine`` conftest fixture resets the globals
    after the test, so the default app-role engine is rebuilt for the
    next test (clean isolation, nothing leaks)."""
    await _reset_engine()
    _db._engine = create_async_engine(_OWNER_DB_URL, pool_pre_ping=True, future=True)
    _db._sessionmaker = async_sessionmaker(
        bind=_db._engine, expire_on_commit=False, autoflush=False
    )
    try:
        yield
    finally:
        await _reset_engine()


async def _reset_engine() -> None:
    if _db._engine is not None:
        await _db._engine.dispose()
    _db._engine = None
    _db._sessionmaker = None


@pytest.fixture
def _s3_via_fake() -> Iterator[FakeAttachmentStore]:
    """Back the store with the Fake via the override ONLY. The override
    short-circuits ``get_attachment_store`` before settings are even
    read, so the global lru_cached ``Settings`` singleton is left
    untouched (mutating it leaks the s3 backend into other tests). The
    migrator's ``attachment_store != 'pg'`` short-circuit is satisfied
    by passing an explicit s3 ``Settings`` at the call site."""
    fake = FakeAttachmentStore()
    set_attachment_store_override(lambda: fake)
    try:
        yield fake
    finally:
        set_attachment_store_override(None)


class _Seed:
    """Ids of one seeded fixture, for scoped self-cleanup (a well-
    behaved test deletes exactly the rows it created so the shared DB
    is never left with an S3-only attachment -- which would, by design,
    block the migration downgrade)."""

    def __init__(
        self,
        att_id: uuid.UUID,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        note_id: uuid.UUID,
    ) -> None:
        self.att_id = att_id
        self.org_id = org_id
        self.user_id = user_id
        self.note_id = note_id


async def _cleanup_seed(seed: _Seed) -> None:
    """Hard-delete exactly the seeded rows (by id). Owner role; runs
    even if the test asserted, so no S3-only row leaks into the shared
    DB. Order respects FKs (attachment -> note -> membership -> org,
    then the user)."""
    async with tenant_session(str(seed.org_id), str(seed.user_id)) as s:
        await s.execute(text("DELETE FROM attachments WHERE id = :i"), {"i": seed.att_id})
        await s.execute(text("DELETE FROM notes WHERE id = :i"), {"i": seed.note_id})
        await s.execute(text("DELETE FROM memberships WHERE org_id = :o"), {"o": seed.org_id})
        await s.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": seed.org_id})
    async with admin_session() as s:
        await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": seed.user_id})


async def _seed_attachment(data: bytes) -> _Seed:
    """A legacy in-DB attachment: bytes in ``data``, ``storage_key``
    NULL. The org+owner go through the SECURITY DEFINER
    ``provision_organization`` (the same RLS-respecting seam production
    signup uses); the org-scoped note + attachment are then inserted in
    a tenant session so the RLS policies allow the writes."""
    async with admin_session() as s:
        user_id = (
            await s.execute(
                text("INSERT INTO users (email, password_hash) VALUES (:e, 'x') RETURNING id"),
                {"e": f"mig_{uuid.uuid4().hex[:8]}@example.test"},
            )
        ).scalar_one()
        org_id = (
            await s.execute(
                text("SELECT provision_organization(:n, :u)"),
                {"n": "MigOrg", "u": str(user_id)},
            )
        ).scalar_one()

    async with tenant_session(str(org_id), str(user_id)) as s:
        note_id = (
            await s.execute(
                text("INSERT INTO notes (org_id, kind) VALUES (:o, 'text') RETURNING id"),
                {"o": org_id},
            )
        ).scalar_one()
        # A note belongs to exactly one client (ADR-0050), enforced by a
        # deferred constraint trigger: this fixture writes the note with
        # raw SQL to exercise the RLS seam, so it has to supply the
        # structural tag itself or the whole transaction aborts at COMMIT.
        # Raw SQL rather than taxonomy.ensure_default_client on purpose:
        # the service audit-logs, and _cleanup_seed's DELETE of the org
        # would then cascade into the append-only activity_log.
        client_tag_id = (
            await s.execute(
                text(
                    "INSERT INTO tags (org_id, kind, name) "
                    "VALUES (:o, 'client', 'MigClient') RETURNING id"
                ),
                {"o": org_id},
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO client_profile (tag_id, org_id, legal_name) "
                "VALUES (:t, :o, 'MigClient')"
            ),
            {"t": client_tag_id, "o": org_id},
        )
        await s.execute(
            text("INSERT INTO note_tags (org_id, note_id, tag_id) VALUES (:o, :n, :t)"),
            {"o": org_id, "n": note_id, "t": client_tag_id},
        )
        att_id = (
            await s.execute(
                text(
                    "INSERT INTO attachments "
                    "(org_id, note_id, filename, mime_type, size_bytes, data, "
                    " uploaded_by) "
                    "VALUES (:o, :n, 'legacy.bin', 'application/octet-stream', "
                    "        :sz, :d, :u) RETURNING id"
                ),
                {
                    "o": org_id,
                    "n": note_id,
                    "sz": len(data),
                    "d": data,
                    "u": user_id,
                },
            )
        ).scalar_one()
        return _Seed(att_id, org_id, user_id, note_id)


async def test_migrator_noop_when_pg() -> None:
    # Default settings => pg => nothing to move, returns 0.
    moved = await migrate_attachments(settings=get_settings())
    assert moved == 0


async def test_migrator_moves_bytes_and_is_idempotent(
    _owner_engine: None,
    _s3_via_fake: FakeAttachmentStore,
) -> None:
    """The real per-workspace migration unit (``_migrate_org`` -- the
    exact body the public ``migrate_attachments`` runs for every org).
    Scoped to the freshly-seeded org so the shared test DB's other
    tenants' rows are left untouched (no collateral mutation)."""
    from mycelium_core.migrate_attachments import _migrate_org

    payload = b"\x00\x01\x02 legacy attachment body \xff"
    seed = await _seed_attachment(payload)
    try:
        moved = await _migrate_org(seed.org_id, seed.user_id, _s3_via_fake, batch_size=50)
        assert moved >= 1

        # Bytes are in the object store under the attachment id key.
        assert await _s3_via_fake.get(str(seed.att_id)) == payload

        async with tenant_session(str(seed.org_id), str(seed.user_id)) as s:
            row = (
                await s.execute(select(Attachment).where(Attachment.id == seed.att_id))
            ).scalar_one()
            assert row.storage_key == str(seed.att_id)
            assert row.data is None

        # Idempotent: a second run over the same org finds nothing left.
        again = await _migrate_org(seed.org_id, seed.user_id, _s3_via_fake, batch_size=50)
        assert again == 0
    finally:
        # Scoped self-cleanup: never leave an S3-only row in the shared
        # DB (it would block the migration downgrade by design).
        await _cleanup_seed(seed)
