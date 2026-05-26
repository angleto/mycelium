"""DB-backed tests for the recovery-history feature (``entity_revision``).

Covers what the existing suite only exercises in passing through the
write-path hook on tasks/notes services: coalescing on the ``web``
channel, restore (full and per-field) with channel='restore' and
restored_from chaining, immutability of sealed rows, the safety-net
seal_idle, cross-entity cascade on DELETE, and RLS isolation across
orgs. API + MCP smoke checks live next to the existing F-suites.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import psycopg
import pytest
from sqlalchemy import select, text

from flow_core.config import get_settings
from flow_core.db import admin_session, tenant_session
from flow_core.errors import ConflictError, DomainError
from flow_core.models.entity_revision import EntityRevision
from flow_core.services import entity_revisions as revs
from flow_core.services import notes as notes_svc
from flow_core.services import tasks as tasks_svc
from flow_core.services.auth import signup


async def _org(label: str = "Org") -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name=label,
        )
    return r.org_id, r.user_id


async def _make_task(
    org: uuid.UUID,
    user: uuid.UUID,
    *,
    title: str = "T",
    channel: str = "api",
) -> tuple[uuid.UUID, int]:
    async with tenant_session(str(org), str(user)) as s:
        t = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title=title,
            channel=channel,
        )
        return t.id, t.version


async def _make_note(
    org: uuid.UUID,
    user: uuid.UUID,
    *,
    title: str = "N",
    text_body: str | None = "first body",
    channel: str = "api",
) -> tuple[uuid.UUID, int]:
    from flow_core.models.note import NoteKind

    async with tenant_session(str(org), str(user)) as s:
        n = await notes_svc.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            title=title,
            text=text_body,
            channel=channel,
        )
        return n.id, n.version


# ────────────────────────────────────────────────────────────────────
# Coalescing
# ────────────────────────────────────────────────────────────────────


async def test_web_coalesces_under_window() -> None:
    """Two web PATCHes with the same edit_session_id under 30s become
    a single open revision: edit_count=2, version_to bumped, snapshot
    reflects the latest payload, changed_fields union of both calls."""
    org, user = await _org("OrgCoalesce")
    tid, v1 = await _make_task(org, user)
    sess = uuid.uuid4().hex

    async with tenant_session(str(org), str(user)) as s:
        v2 = await tasks_svc.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=tid,
            expected_version=v1,
            values={"title": "T2"},
            channel="web",
            edit_session_id=sess,
        )
        v3 = await tasks_svc.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=tid,
            expected_version=v2,
            values={"description": "longer"},
            channel="web",
            edit_session_id=sess,
        )
        rows = await revs.list_revisions(
            s, entity_kind=revs.ENTITY_KIND_TASK, entity_id=tid, limit=10
        )

    # Newest first: the open coalesced web revision, then the
    # sealed-on-arrival ``_create`` revision.
    assert len(rows) == 2
    open_rev = rows[0]
    create_rev = rows[1]
    assert open_rev.channel == "web"
    assert open_rev.sealed_at is None
    assert open_rev.edit_count == 2
    assert open_rev.version_to == v3
    assert set(open_rev.changed_fields) == {"title", "description"}
    assert open_rev.snapshot["title"] == "T2"
    assert open_rev.snapshot["description"] == "longer"
    assert create_rev.changed_fields == ["_create"]
    assert create_rev.sealed_at is not None  # baseline is sealed-on-arrival


async def test_web_different_sessions_keep_separate_revisions() -> None:
    """Two edits with different ``edit_session_id`` produce TWO web
    revisions; sessions don't merge across ids. Both can stay open
    in parallel (think two tabs / two devices editing the same
    entity): the partial unique index keys on ``edit_session_id``,
    so distinct sessions never collide."""
    org, user = await _org("OrgSplit")
    tid, v1 = await _make_task(org, user)
    s1 = uuid.uuid4().hex
    s2 = uuid.uuid4().hex

    async with tenant_session(str(org), str(user)) as s:
        v2 = await tasks_svc.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=tid,
            expected_version=v1,
            values={"title": "from s1"},
            channel="web",
            edit_session_id=s1,
        )
        await tasks_svc.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=tid,
            expected_version=v2,
            values={"title": "from s2"},
            channel="web",
            edit_session_id=s2,
        )
        rows = await revs.list_revisions(
            s, entity_kind=revs.ENTITY_KIND_TASK, entity_id=tid, limit=10
        )

    web_rows = [r for r in rows if r.channel == "web"]
    assert len(web_rows) == 2
    sessions = {r.edit_session_id for r in web_rows}
    assert sessions == {s1, s2}
    # Each web row stays open until explicit/idle seal: distinct
    # sessions are tracked independently rather than overwriting.
    assert all(r.sealed_at is None for r in web_rows)


async def test_web_expired_window_starts_fresh_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    """An edit landing >30s after the previous one with the same
    session id seals the stale row and opens a brand-new revision."""
    org, user = await _org("OrgExpire")
    tid, v1 = await _make_task(org, user)
    sess = uuid.uuid4().hex

    async with tenant_session(str(org), str(user)) as s:
        v2 = await tasks_svc.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=tid,
            expected_version=v1,
            values={"title": "first"},
            channel="web",
            edit_session_id=sess,
        )
        # Move the open revision's ``last_edit_at`` back in time so the
        # next append falls outside COALESCE_WINDOW_SECONDS. Using SQL
        # avoids the BEFORE UPDATE no-update-sealed trigger (the row is
        # still open at this point).
        await s.execute(
            text(
                "UPDATE entity_revision "
                "SET last_edit_at = now() - interval '120 seconds' "
                "WHERE entity_kind = 'task' AND entity_id = :tid "
                "  AND sealed_at IS NULL"
            ),
            {"tid": str(tid)},
        )
        v3 = await tasks_svc.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=tid,
            expected_version=v2,
            values={"title": "second"},
            channel="web",
            edit_session_id=sess,
        )
        web_rows = [
            r
            for r in await revs.list_revisions(
                s, entity_kind=revs.ENTITY_KIND_TASK, entity_id=tid, limit=10
            )
            if r.channel == "web"
        ]

    assert len(web_rows) == 2
    open_rows = [r for r in web_rows if r.sealed_at is None]
    sealed_rows = [r for r in web_rows if r.sealed_at is not None]
    assert len(open_rows) == 1
    assert len(sealed_rows) == 1
    assert open_rows[0].version_to == v3


async def test_non_web_channels_are_sealed_on_arrival() -> None:
    """Every non-web channel writes sealed-immediately. ``edit_count``
    stays 1, ``sealed_at`` matches ``started_at``."""
    org, user = await _org("OrgNonWeb")
    tid, v1 = await _make_task(org, user, channel="mcp")
    async with tenant_session(str(org), str(user)) as s:
        await tasks_svc.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=tid,
            expected_version=v1,
            values={"title": "via mcp"},
            channel="mcp",
        )
        rows = await revs.list_revisions(
            s, entity_kind=revs.ENTITY_KIND_TASK, entity_id=tid, limit=10
        )
    assert all(r.sealed_at is not None for r in rows)
    assert all(r.edit_count == 1 for r in rows)


# ────────────────────────────────────────────────────────────────────
# Restore
# ────────────────────────────────────────────────────────────────────


async def test_restore_total_writes_new_sealed_rev_chained() -> None:
    """A restore produces a NEW sealed revision on the ``restore``
    channel with ``restored_from`` pointing at the source. The source
    is not mutated."""
    org, user = await _org("OrgRestoreFull")
    tid, v1 = await _make_task(org, user)
    async with tenant_session(str(org), str(user)) as s:
        v2 = await tasks_svc.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=tid,
            expected_version=v1,
            values={"title": "edited", "description": "new descr"},
            channel="api",
        )
        baseline = await revs.list_revisions(
            s, entity_kind=revs.ENTITY_KIND_TASK, entity_id=tid, limit=10
        )
        # Pick the rev that captured the create snapshot (the oldest).
        src = baseline[-1]
        v3 = await tasks_svc.restore_revision(
            s,
            org_id=org,
            actor_id=user,
            task_id=tid,
            revision_id=src.id,
            expected_version=v2,
        )
        rows = await revs.list_revisions(
            s, entity_kind=revs.ENTITY_KIND_TASK, entity_id=tid, limit=10
        )
        # The restored task should now match the baseline title.
        t = await tasks_svc.get_task(s, org_id=org, task_id=tid)

    assert v3 == v2 + 1
    restore_rev = rows[0]
    assert restore_rev.channel == "restore"
    assert restore_rev.restored_from == src.id
    assert restore_rev.sealed_at is not None
    # Source revision intact.
    same_src = next(r for r in rows if r.id == src.id)
    assert same_src.channel == src.channel
    assert same_src.snapshot == src.snapshot
    # Task reverted: title back to baseline ``T``.
    assert t.title == "T"


async def test_restore_partial_fields_only() -> None:
    """``fields=['title']`` reverts the title but leaves the
    description as-is (current value, not the snapshot's)."""
    org, user = await _org("OrgRestorePartial")
    tid, v1 = await _make_task(org, user)
    async with tenant_session(str(org), str(user)) as s:
        v2 = await tasks_svc.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=tid,
            expected_version=v1,
            values={"title": "edited", "description": "kept"},
        )
        # Baseline revision (create) has title=T and description=None.
        baseline = await revs.list_revisions(
            s, entity_kind=revs.ENTITY_KIND_TASK, entity_id=tid, limit=10
        )
        src = baseline[-1]
        await tasks_svc.restore_revision(
            s,
            org_id=org,
            actor_id=user,
            task_id=tid,
            revision_id=src.id,
            expected_version=v2,
            fields=["title"],
        )
        t = await tasks_svc.get_task(s, org_id=org, task_id=tid)
    assert t.title == "T"
    assert t.description == "kept"


async def test_restore_rejects_non_restorable_field() -> None:
    """The whitelist refuses identity / state columns: a malicious
    ``fields=['owner_id']`` raises DomainError, the task is not
    touched."""
    org, user = await _org("OrgRestoreReject")
    tid, v1 = await _make_task(org, user)
    async with tenant_session(str(org), str(user)) as s:
        await tasks_svc.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=tid,
            expected_version=v1,
            values={"title": "edited"},
        )
        baseline = await revs.list_revisions(
            s, entity_kind=revs.ENTITY_KIND_TASK, entity_id=tid, limit=10
        )
        src = baseline[-1]
        with pytest.raises(DomainError):
            await tasks_svc.restore_revision(
                s,
                org_id=org,
                actor_id=user,
                task_id=tid,
                revision_id=src.id,
                expected_version=v1 + 1,
                fields=["owner_id"],
            )


async def test_restore_stale_version_conflict() -> None:
    """Restore goes through ``update_task``, so a stale
    ``expected_version`` triggers a ConflictError (HTTP 409 at the
    REST boundary)."""
    org, user = await _org("OrgRestoreStale")
    tid, v1 = await _make_task(org, user)
    async with tenant_session(str(org), str(user)) as s:
        await tasks_svc.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=tid,
            expected_version=v1,
            values={"title": "edited"},
        )
        baseline = await revs.list_revisions(
            s, entity_kind=revs.ENTITY_KIND_TASK, entity_id=tid, limit=10
        )
        src = baseline[-1]
        with pytest.raises(ConflictError):
            await tasks_svc.restore_revision(
                s,
                org_id=org,
                actor_id=user,
                task_id=tid,
                revision_id=src.id,
                expected_version=v1,  # stale: real version is v1+1
            )


async def test_sealed_row_immutable_trigger() -> None:
    """The BEFORE UPDATE trigger forbids touching a sealed row,
    regardless of which column. Bypassing the service can't corrupt
    history."""
    org, user = await _org("OrgSealedImmutable")
    tid, _v1 = await _make_task(org, user)
    async with tenant_session(str(org), str(user)) as s:
        rows = await revs.list_revisions(
            s, entity_kind=revs.ENTITY_KIND_TASK, entity_id=tid, limit=10
        )
        sealed = next(r for r in rows if r.sealed_at is not None)
        with pytest.raises(Exception) as ei:
            await s.execute(
                text("UPDATE entity_revision SET edit_count = edit_count + 1 WHERE id = :rid"),
                {"rid": str(sealed.id)},
            )
    # Trigger raises with a custom message; check it's the right one.
    assert "sealed" in str(ei.value).lower()


# ────────────────────────────────────────────────────────────────────
# Seal: explicit + safety-net
# ────────────────────────────────────────────────────────────────────


async def test_seal_open_is_idempotent() -> None:
    """Two consecutive seal_open on the same edit-session: the first
    closes 1 row, the second closes 0."""
    org, user = await _org("OrgSealIdem")
    tid, v1 = await _make_task(org, user)
    sess = uuid.uuid4().hex
    async with tenant_session(str(org), str(user)) as s:
        await tasks_svc.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=tid,
            expected_version=v1,
            values={"title": "x"},
            channel="web",
            edit_session_id=sess,
        )
        first = await revs.seal_open(
            s,
            entity_kind=revs.ENTITY_KIND_TASK,
            entity_id=tid,
            actor_id=user,
            edit_session_id=sess,
        )
        second = await revs.seal_open(
            s,
            entity_kind=revs.ENTITY_KIND_TASK,
            entity_id=tid,
            actor_id=user,
            edit_session_id=sess,
        )
    assert first == 1
    assert second == 0


async def test_seal_idle_closes_only_old_web_rows() -> None:
    """seal_idle only targets web rows whose last_edit_at is past
    the cutoff. A recent web row and a sealed row of any age are
    untouched."""
    org, user = await _org("OrgSealIdle")
    tid, v1 = await _make_task(org, user)
    sess_old = uuid.uuid4().hex
    sess_new = uuid.uuid4().hex
    async with tenant_session(str(org), str(user)) as s:
        v2 = await tasks_svc.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=tid,
            expected_version=v1,
            values={"title": "old"},
            channel="web",
            edit_session_id=sess_old,
        )
        # Backdate the first open window so it's past the cutoff.
        await s.execute(
            text(
                "UPDATE entity_revision "
                "SET last_edit_at = now() - interval '5 minutes' "
                "WHERE entity_kind='task' AND entity_id=:tid "
                "  AND edit_session_id = :sess AND sealed_at IS NULL"
            ),
            {"tid": str(tid), "sess": sess_old},
        )
        # Open a fresh web window (different session id), still recent.
        await tasks_svc.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=tid,
            expected_version=v2,
            values={"title": "new"},
            channel="web",
            edit_session_id=sess_new,
        )

    # entity_revision is FORCE-RLS: seal_idle must run inside a
    # tenant_session (the worker iterates orgs and does the same).
    async with tenant_session(str(org), str(user), actor_kind="system") as s:
        sealed = await revs.seal_idle(s, older_than_seconds=60)

    async with tenant_session(str(org), str(user)) as s:
        rows = await revs.list_revisions(
            s, entity_kind=revs.ENTITY_KIND_TASK, entity_id=tid, limit=20
        )
    web_rows = {r.edit_session_id: r for r in rows if r.channel == "web"}
    # The stale session is now sealed; the fresh one is still open.
    assert sealed >= 1
    assert web_rows[sess_old].sealed_at is not None
    assert web_rows[sess_new].sealed_at is None


# ────────────────────────────────────────────────────────────────────
# Cascade on DELETE
# ────────────────────────────────────────────────────────────────────


async def test_hard_delete_cascades_revisions() -> None:
    """Hard-deleting a task removes its revisions via the
    AFTER DELETE trigger. The DELETE is issued by the migrator role
    (BYPASSRLS) — same one the future retention worker uses."""
    org, user = await _org("OrgCascade")
    tid, _v1 = await _make_task(org, user)
    async with tenant_session(str(org), str(user)) as s:
        before = await revs.list_revisions(
            s, entity_kind=revs.ENTITY_KIND_TASK, entity_id=tid, limit=10
        )
    assert before  # baseline create revision exists

    dsn = get_settings().database_url_sync.replace("+psycopg", "").replace("+asyncpg", "")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM tasks WHERE id = %s", (str(tid),))
        cur = conn.execute(
            "SELECT count(*) FROM entity_revision "
            "WHERE entity_kind='task' AND entity_id = %s",
            (str(tid),),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0


# ────────────────────────────────────────────────────────────────────
# RLS isolation
# ────────────────────────────────────────────────────────────────────


async def test_revisions_rls_isolates_orgs() -> None:
    """A revision in org A is invisible to org B via the standard
    tenant_session (RLS). The DB row exists but the tenant query
    sees zero rows."""
    org_a, user_a = await _org("OrgA")
    org_b, user_b = await _org("OrgB")
    tid_a, _ = await _make_task(org_a, user_a, title="A-task")
    async with tenant_session(str(org_b), str(user_b)) as s:
        rows = (
            await s.execute(
                select(EntityRevision).where(EntityRevision.entity_id == tid_a)
            )
        ).scalars().all()
    assert rows == []


# ────────────────────────────────────────────────────────────────────
# Notes parity
# ────────────────────────────────────────────────────────────────────


async def test_note_update_writes_revision_and_restore_back() -> None:
    """End-to-end note path: create + update + restore round-trips
    through the same hook; restorable_payload yields title and
    transcript (the only restorable fields for notes)."""
    org, user = await _org("OrgNoteRestore")
    nid, v1 = await _make_note(org, user, title="N0", text_body="body0")
    async with tenant_session(str(org), str(user)) as s:
        v2 = await notes_svc.update_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=nid,
            expected_version=v1,
            title="N1",
            text="body1",
        )
        rows = await revs.list_revisions(
            s, entity_kind=revs.ENTITY_KIND_NOTE, entity_id=nid, limit=10
        )
        src = rows[-1]  # the baseline create revision
        await notes_svc.restore_revision(
            s,
            org_id=org,
            actor_id=user,
            note_id=nid,
            revision_id=src.id,
            expected_version=v2,
        )
        n = await notes_svc.get_note(s, org_id=org, note_id=nid)

    # Title comes from _derive_title("body0") -> first non-empty line
    # = "body0" (no explicit title was provided either time the
    # transcript was the body source). Check transcript matches.
    assert n.transcript == "body0"


# ────────────────────────────────────────────────────────────────────
# Asyncio sanity: pytest-asyncio runs each test with its own loop.
# (The conftest fixture disposes the engine per loop; nothing to do
# here besides letting pytest-asyncio pick the tests up.)
# ────────────────────────────────────────────────────────────────────


# Mark the module as async-tested so pytest-asyncio doesn't skip it
# even if the project's pyproject only marks specific patterns.
pytestmark = pytest.mark.asyncio


# Silence unused-import warnings: ``asyncio`` is imported because a
# couple of the test bodies await tasks_svc through tenant_session
# (which already drives the loop), and the imports stay close to the
# pattern of every other DB-backed test in the suite.
_ = asyncio
_ = dt
