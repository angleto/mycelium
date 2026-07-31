"""The rekey tool decides by comparing the stored key against the key
the row SHOULD have -- never against the key's shape.

The regression that held a deploy is ``test_wrong_client_key_is_rekeyed``:
after migration 0086 moved entities to another client (ADR-0050), their
attachments kept a perfectly hierarchical key under the PREVIOUS
client, and the old ``startswith("org/")`` skip declared those rows
already correct.

Same runtime contract as ``test_migrate_attachments``: the tool walks
workspace by workspace inside a ``tenant_session``, so it needs no
BYPASSRLS role and the tests need no engine swap. Each call is scoped
with ``org_ids=[seed.org]``: the tallies are then about the rows the
test seeded, not about whatever else a shared database happens to hold
(asserting a global count here failed the moment the suite had run
once and left 4840 attachments behind).

Bytes go through the in-memory Fake store (no boto3, no network); the
S3-only rows the tests create are swept by the autouse
``_purge_s3_only_attachments`` conftest net.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from _fake_attachment_store import FakeAttachmentStore
from sqlalchemy import select, text

import mycelium_core.db as _db
from mycelium_core.attachment_store import set_attachment_store_override
from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.attachment import Attachment
from mycelium_core.rekey_attachments import rekey_attachments
from mycelium_core.services import attachments as attachments_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services import taxonomy
from mycelium_core.services.auth import signup
from mycelium_core.services.taxonomy import ClientInput

# Owner (BYPASSRLS) DSN, derived from the configured sync DSN so the
# test honours MYCELIUM_DATABASE_URL_SYNC instead of hardcoding a host;
# only the driver is swapped for the async engine.
_OWNER_DB_URL = get_settings().database_url_sync.replace("+psycopg", "+asyncpg")

_PAYLOAD = b"\x00\x01 rekey me \xff"
_FILENAME = "plan.bin"


async def _reset_engine() -> None:
    if _db._engine is not None:
        await _db._engine.dispose()
    _db._engine = None
    _db._sessionmaker = None


@pytest.fixture
def _store() -> Iterator[FakeAttachmentStore]:
    """Back the attachment store with the Fake via the override ONLY:
    it short-circuits ``get_attachment_store`` before settings are read,
    so the lru_cached Settings singleton is left alone (mutating it
    leaks the s3 backend into other tests)."""
    fake = FakeAttachmentStore()
    set_attachment_store_override(lambda: fake)
    try:
        yield fake
    finally:
        set_attachment_store_override(None)


@dataclass
class _Seed:
    """One workspace with two client -> project chains, a task on the
    first chain, and one attachment on that task whose object is in the
    Fake store under ``key``."""

    org: uuid.UUID
    user: uuid.UUID
    task: uuid.UUID
    att: uuid.UUID
    key: str
    c1: uuid.UUID
    c2: uuid.UUID
    p2: uuid.UUID


async def _seed(store: FakeAttachmentStore) -> _Seed:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="REKEY",
        )
    org, user = r.org_id, r.user_id
    async with tenant_session(str(org), str(user)) as s:
        c1 = await taxonomy.create_client(
            s, org_id=org, actor_id=user, name="Cee", profile=ClientInput(legal_name="Cee SRL")
        )
        p1 = await taxonomy.create_project(
            s, org_id=org, actor_id=user, name="Roof", client_tag_id=c1.id
        )
        c2 = await taxonomy.create_client(
            s, org_id=org, actor_id=user, name="Dee", profile=ClientInput(legal_name="Dee SRL")
        )
        p2 = await taxonomy.create_project(
            s, org_id=org, actor_id=user, name="Cellar", client_tag_id=c2.id
        )
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="T", tag_ids=[p1.id])
        att = await attachments_svc.add_attachment(
            s,
            org_id=org,
            actor_id=user,
            task_id=task.id,
            filename=_FILENAME,
            mime_type="application/octet-stream",
            data=_PAYLOAD,
        )
        key = att.storage_key
    # The upload path is the reference implementation of "correct".
    assert key == f"org/{org}/client/{c1.id}/tasks/{task.id}/{att.id}/{_FILENAME}"
    assert await store.get(key) == _PAYLOAD
    return _Seed(
        org=org, user=user, task=task.id, att=att.id, key=key, c1=c1.id, c2=c2.id, p2=p2.id
    )


async def _stored_key(seed: _Seed) -> str | None:
    """The row's key read back through the ordinary tenant (RLS) path."""
    async with tenant_session(str(seed.org), str(seed.user)) as s:
        row = (await s.execute(select(Attachment).where(Attachment.id == seed.att))).scalar_one()
        return row.storage_key


async def test_flat_legacy_key_is_rekeyed(_store: FakeAttachmentStore) -> None:
    """The pre-hierarchy shape: storage_key is the bare attachment id."""
    seed = await _seed(_store)
    async with tenant_session(str(seed.org), str(seed.user)) as s:
        await s.execute(
            text("UPDATE attachments SET storage_key = :k WHERE id = :i"),
            {"k": str(seed.att), "i": seed.att},
        )
    _store.objects[str(seed.att)] = _store.objects.pop(seed.key)

    stats = await rekey_attachments(_store, org_ids=[seed.org])

    assert stats.rekeyed == 1
    assert stats.errors == 0
    # The client never changed, so the expected key is the one the
    # upload path had built.
    assert await _stored_key(seed) == seed.key
    assert await _store.get(seed.key) == _PAYLOAD
    assert str(seed.att) not in _store.objects


async def test_wrong_client_key_is_rekeyed(_store: FakeAttachmentStore) -> None:
    """THE regression. The key is hierarchical and well-formed, and
    still wrong: it names the client the task no longer belongs to.
    Moving the task to another project is a MOVE of its client
    (services/tag_assignment) -- the same transition migration 0086
    applied in bulk."""
    seed = await _seed(_store)
    async with tenant_session(str(seed.org), str(seed.user)) as s:
        await tasks_svc.attach_tag(
            s, org_id=seed.org, actor_id=seed.user, task_id=seed.task, tag_id=seed.p2
        )
    assert await _stored_key(seed) == seed.key  # still under c1

    stats = await rekey_attachments(_store, org_ids=[seed.org])

    expected = f"org/{seed.org}/client/{seed.c2}/tasks/{seed.task}/{seed.att}/{_FILENAME}"
    assert stats.rekeyed == 1
    assert stats.errors == 0
    assert await _stored_key(seed) == expected
    assert await _store.get(expected) == _PAYLOAD
    # Nothing is left in the previous client's folder.
    assert seed.key not in _store.objects
    assert not [k for k in _store.objects if str(seed.c1) in k]


async def test_correct_key_is_left_alone_and_counted(_store: FakeAttachmentStore) -> None:
    """A row already on its expected key is skipped as CORRECT, not as
    pg-backed and not as an error -- and nothing is touched. This is
    also the idempotence guarantee: it is the state every rekeyed row
    is left in."""
    seed = await _seed(_store)
    before = dict(_store.objects)

    stats = await rekey_attachments(_store, org_ids=[seed.org])

    assert stats.rekeyed == 0
    assert stats.errors == 0
    # >= 1, not == 1: the scan is cross-tenant, so any other correctly
    # keyed row in the shared test DB is counted here too.
    assert stats.skipped_correct >= 1
    assert await _stored_key(seed) == seed.key
    assert _store.objects == before


async def test_dry_run_mutates_nothing(_store: FakeAttachmentStore) -> None:
    """--dry-run reports the moves it would make and writes neither the
    row nor the object store."""
    seed = await _seed(_store)
    async with tenant_session(str(seed.org), str(seed.user)) as s:
        await tasks_svc.attach_tag(
            s, org_id=seed.org, actor_id=seed.user, task_id=seed.task, tag_id=seed.p2
        )
    before = dict(_store.objects)

    stats = await rekey_attachments(_store, dry_run=True, org_ids=[seed.org])

    assert stats.rekeyed == 1  # would rekey
    assert stats.errors == 0
    assert await _stored_key(seed) == seed.key
    assert _store.objects == before
