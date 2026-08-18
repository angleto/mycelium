"""DB-backed tests for the recovery-history feature (``entity_revision``).

Covers what the existing suite only exercises in passing through the
write-path hook on tasks/notes services: coalescing on the ``web``
channel, restore (full and per-field) with channel='restore' and
restored_from chaining, immutability of sealed rows, the safety-net
seal_idle, cross-entity cascade on DELETE, and RLS isolation across
orgs. API + MCP smoke checks live next to the existing F-suites.
"""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg
import pytest
from sqlalchemy import select, text

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import ConflictError, DomainError
from mycelium_core.models.entity_revision import EntityRevision
from mycelium_core.services import entity_revisions as revs
from mycelium_core.services import notes as notes_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup


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
    from mycelium_core.models.note import NoteKind

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


def test_snapshot_whitelist_is_subset_of_model_columns() -> None:
    """The snapshot whitelist is hardcoded by design (safe-by-default
    against new privacy-sensitive columns) but a column rename or
    removal would silently drop it from snapshots; catch that drift
    here so the test fails when the schema and the whitelist diverge.

    Symmetric for task and note.
    """
    from mycelium_core.models.note import Note
    from mycelium_core.models.task import Task
    from mycelium_core.services.entity_revisions import (
        _NOTE_SNAPSHOT_FIELDS,
        _TASK_SNAPSHOT_FIELDS,
    )

    task_cols = {c.name for c in Task.__table__.columns}
    note_cols = {c.name for c in Note.__table__.columns}
    # Phase 6 final (task 1cd8bc0a): ``transcript`` left the Note row
    # in migration 0012; it stays in _NOTE_SNAPSHOT_FIELDS as a
    # virtual key filled by snapshot_note from note_part(ord=0)+.
    note_virtual = {"transcript"}
    task_missing = set(_TASK_SNAPSHOT_FIELDS) - task_cols
    note_missing = set(_NOTE_SNAPSHOT_FIELDS) - note_cols - note_virtual
    assert not task_missing, (
        f"_TASK_SNAPSHOT_FIELDS references non-existent columns: {task_missing}"
    )
    assert not note_missing, (
        f"_NOTE_SNAPSHOT_FIELDS references non-existent columns: {note_missing}"
    )


def test_restorable_whitelist_is_subset_of_snapshot() -> None:
    """Every restorable field must be in the snapshot (otherwise a
    restore would pull from a missing key). Snapshot ⊇ Restorable is
    the load-bearing invariant."""
    from mycelium_core.services.entity_revisions import (
        _NOTE_RESTORABLE_FIELDS,
        _NOTE_SNAPSHOT_FIELDS,
        _TASK_RESTORABLE_FIELDS,
        _TASK_SNAPSHOT_FIELDS,
    )

    task_orphan = _TASK_RESTORABLE_FIELDS - set(_TASK_SNAPSHOT_FIELDS)
    note_orphan = _NOTE_RESTORABLE_FIELDS - set(_NOTE_SNAPSHOT_FIELDS)
    assert not task_orphan, f"Task restorable fields missing from snapshot: {task_orphan}"
    assert not note_orphan, f"Note restorable fields missing from snapshot: {note_orphan}"


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


async def test_web_expired_window_starts_fresh_revision() -> None:
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
    """The BEFORE UPDATE trigger forbids touching any column on a
    sealed row EXCEPT ``summary`` (column allow-list since migration
    0010). Bypassing the service can't corrupt history.
    """
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


async def test_summary_settable_on_sealed_row() -> None:
    """The summary column escapes the sealed-immutability gate so the
    LLM sweep (and the user, via PATCH) can label sealed revisions
    after the fact. Empty / whitespace inputs collapse to NULL; long
    strings get trimmed to ``SUMMARY_MAX_LEN``."""
    org, user = await _org("OrgRevisionSummary")
    tid, _v1 = await _make_task(org, user)
    async with tenant_session(str(org), str(user)) as s:
        rows = await revs.list_revisions(
            s, entity_kind=revs.ENTITY_KIND_TASK, entity_id=tid, limit=10
        )
        sealed = next(r for r in rows if r.sealed_at is not None)
        assert sealed.summary is None

        # Plain set.
        out = await revs.set_summary(
            s,
            revision_id=sealed.id,
            summary="renamed task, dropped cost",
            entity_kind=revs.ENTITY_KIND_TASK,
            entity_id=tid,
        )
        assert out.summary == "renamed task, dropped cost"

        # Whitespace / empty -> NULL.
        out = await revs.set_summary(s, revision_id=sealed.id, summary="   ")
        assert out.summary is None

        # None explicit clear.
        await revs.set_summary(s, revision_id=sealed.id, summary="x")
        out = await revs.set_summary(s, revision_id=sealed.id, summary=None)
        assert out.summary is None

        # Trim to SUMMARY_MAX_LEN.
        long = "a" * (revs.SUMMARY_MAX_LEN + 50)
        out = await revs.set_summary(s, revision_id=sealed.id, summary=long)
        assert out.summary is not None
        assert len(out.summary) == revs.SUMMARY_MAX_LEN


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
            "SELECT count(*) FROM entity_revision WHERE entity_kind='task' AND entity_id = %s",
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
            (await s.execute(select(EntityRevision).where(EntityRevision.entity_id == tid_a)))
            .scalars()
            .all()
        )
    assert rows == []


# ────────────────────────────────────────────────────────────────────
# Notes parity
# ────────────────────────────────────────────────────────────────────


async def test_coarsen_keeps_one_per_day_then_one_per_week() -> None:
    """Coarsening collapses sealed revisions older than the
    retain-full window down to one per (entity, day) up to the
    weekly cutoff, then one per (entity, week) past it. Untouched:
    the retain-full window and the open web revision."""
    org, user = await _org("OrgCoarsen")
    tid, _v = await _make_task(org, user)

    async with tenant_session(str(org), str(user)) as s:
        # Hand-craft a synthetic timeline of sealed revisions covering
        # three "old" days inside the daily zone and two old weeks
        # past it. We bypass append() and write straight to the DB so
        # the timestamps land in the past (append() always uses
        # now()).
        # (ts_offset, expected_kept?). asyncpg binds Python timedelta
        # as a PG interval directly, so we skip the text cast.
        rows: list[tuple[dt.timedelta, int]] = [
            # 3 rows on day-50: only the most recent must survive
            (dt.timedelta(days=50), 1),
            (dt.timedelta(days=50, hours=1), 1),
            (dt.timedelta(days=50, hours=2), 1),
            # 2 rows on day-60: only the most recent must survive
            (dt.timedelta(days=60), 1),
            (dt.timedelta(days=60, hours=5), 1),
            # 1 row on day-400 (past the weekly cutoff, alone in its week)
            (dt.timedelta(days=400), 1),
            # 3 rows in the same ISO-week (~day 405..407): only one survives
            (dt.timedelta(days=405), 1),
            (dt.timedelta(days=406), 1),
            (dt.timedelta(days=407), 1),
        ]
        # asyncpg infers each bind's PG type from the column it lands
        # in; subtracting an interval at the SQL level confuses it
        # because it expects $4 to already be a timestamptz. Pre-compute
        # the absolute timestamp in Python and bind that directly.
        # Anchored to MIDDAY, not to the real clock. The offsets below carry
        # hours (day-60 minus 5h) to put several revisions inside one day, and
        # subtracting them from a "now" that is itself early in the morning
        # lands them on the PREVIOUS day -- so the rows stop grouping and the
        # test fails for everyone who runs it between 00:00 and 05:00 UTC.
        # A 12h shift changes no 30/365-day window boundary here.
        now = dt.datetime.now(tz=dt.UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        for offset, _kept in rows:
            ts = now - offset
            await s.execute(
                text(
                    """
                    INSERT INTO entity_revision
                      (id, org_id, entity_kind, entity_id, snapshot,
                       changed_fields, channel, actor_id, actor_kind,
                       version_from, version_to, edit_count,
                       started_at, last_edit_at, sealed_at)
                    VALUES (
                      gen_random_uuid(), :org, 'task', :tid,
                      '{"title":"T"}'::jsonb,
                      ARRAY['title']::text[], 'api',
                      :uid, 'human_direct', 1, 1, 1,
                      :ts, :ts, :ts
                    )
                    """
                ),
                {
                    "org": str(org),
                    "tid": str(tid),
                    "uid": str(user),
                    "ts": ts,
                },
            )

        before = (
            await s.execute(
                text(
                    "SELECT count(*) FROM entity_revision "
                    "WHERE entity_kind='task' AND entity_id = :tid"
                ),
                {"tid": str(tid)},
            )
        ).scalar_one()
        daily, weekly = await revs.coarsen(
            s,
            retain_full_days=30,
            coarse_to_weekly_days=365,
        )
        after = (
            await s.execute(
                text(
                    "SELECT count(*) FROM entity_revision "
                    "WHERE entity_kind='task' AND entity_id = :tid"
                ),
                {"tid": str(tid)},
            )
        ).scalar_one()

    # Daily zone (30..365 days): 3 rows on day-50 → 2 deleted;
    # 2 rows on day-60 → 1 deleted. Total daily: 3.
    assert daily == 3
    # Weekly zone (>365 days): 3 rows in one ISO-week of day-405..407
    # → 2 deleted; the lone day-400 row sits in a different week so
    # it survives. Total weekly: 2.
    assert weekly == 2
    assert after == before - (daily + weekly)


async def test_coarsen_keeps_create_baseline_inside_retain_window() -> None:
    """A freshly-created task has a single sealed revision in the
    retain-full window: coarsening must not touch it."""
    org, user = await _org("OrgCoarsenRecent")
    tid, _v = await _make_task(org, user)
    async with tenant_session(str(org), str(user)) as s:
        daily, weekly = await revs.coarsen(s, retain_full_days=30, coarse_to_weekly_days=365)
        rows = await revs.list_revisions(
            s, entity_kind=revs.ENTITY_KIND_TASK, entity_id=tid, limit=10
        )
    assert daily == 0
    assert weekly == 0
    assert len(rows) == 1


async def test_hard_delete_soft_deleted_cascades_revisions() -> None:
    """A task soft-deleted past the cutoff is hard-deleted, and the
    cascade trigger purges its revisions in the same transaction."""
    org, user = await _org("OrgHardDelete")
    tid, v1 = await _make_task(org, user)
    async with tenant_session(str(org), str(user)) as s:
        # Soft-delete the task first (the normal user flow).
        await tasks_svc.soft_delete_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=tid,
            expected_version=v1,
        )
        # Backdate the soft-delete so it qualifies as "expired".
        await s.execute(
            text("UPDATE tasks SET deleted_at = now() - interval '120 days' WHERE id = :tid"),
            {"tid": str(tid)},
        )
        tasks_d, notes_d = await revs.hard_delete_soft_deleted(s, after_days=90)
        # Both the task row and its revisions are gone.
        cnt_task = (
            await s.execute(
                text("SELECT count(*) FROM tasks WHERE id = :tid"),
                {"tid": str(tid)},
            )
        ).scalar_one()
        cnt_rev = (
            await s.execute(
                text(
                    "SELECT count(*) FROM entity_revision "
                    "WHERE entity_kind='task' AND entity_id = :tid"
                ),
                {"tid": str(tid)},
            )
        ).scalar_one()
    assert tasks_d == 1
    assert notes_d == 0
    assert cnt_task == 0
    assert cnt_rev == 0


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
        # Phase 6 final: the canonical body lives in note_part(ord=0)
        # after the column drop. Read it back via the helper.
        body = await notes_svc.get_body(s, note_id=nid)

    # Title comes from _derive_title("body0") -> first non-empty line
    # = "body0" (no explicit title was provided either time the body
    # was source). Check the restored body matches the snapshot.
    assert n.title == "N0"
    assert body == "body0"


# Pytest-asyncio is configured ``mode=auto`` in pyproject; every
# coroutine in this module is collected as an async test
# automatically and the autouse ``_dispose_engine`` fixture in
# conftest resets the async engine across loops. No explicit
# ``pytestmark`` (using one would also mark the sync whitelist
# tests, raising a warning).


async def test_hard_delete_spares_humus_originals() -> None:
    """WS-F1 / ADR-0041: the autonomous retention sweep never hard-deletes
    an original. A humus source (a ``hypha_of`` parent) and a humus-flagged
    note are spared past the cutoff; an ordinary soft-deleted note is purged.
    """
    from sqlalchemy import text

    from mycelium_core.services import note_links

    org, user = await _org("OrgRetain")
    src, _ = await _make_note(org, user, title="source")
    atom, _ = await _make_note(org, user, title="distillation")
    plain, _ = await _make_note(org, user, title="plain")
    async with tenant_session(str(org), str(user)) as s:
        # src -> atom (hypha_of): src has a derived node. atom is humus.
        await note_links.link_notes(
            s, org_id=org, actor_id=user, parent_note_id=src, child_note_id=atom, kind="hypha_of"
        )
        await s.execute(
            text("UPDATE notes SET humus_flag = true WHERE id = :id"), {"id": str(atom)}
        )
        # Soft-delete all three (re-read version each time to be safe).
        for nid in (src, atom, plain):
            note = await notes_svc.get_note(s, org_id=org, note_id=nid)
            await notes_svc.soft_delete_note(
                s, org_id=org, actor_id=user, note_id=nid, expected_version=note.version
            )
        # Backdate every soft-deleted note in the org past the cutoff.
        await s.execute(
            text(
                "UPDATE notes SET deleted_at = now() - interval '120 days' "
                "WHERE deleted_at IS NOT NULL"
            )
        )
        _tasks_d, notes_d = await revs.hard_delete_soft_deleted(s, after_days=90)

        async def _exists(nid: uuid.UUID) -> int:
            return (
                await s.execute(text("SELECT count(*) FROM notes WHERE id = :id"), {"id": str(nid)})
            ).scalar_one()

        plain_cnt = await _exists(plain)
        src_cnt = await _exists(src)
        atom_cnt = await _exists(atom)
    assert notes_d == 1  # only the ordinary note
    assert plain_cnt == 0  # purged
    assert src_cnt == 1  # spared: hypha_of source (has derived nodes)
    assert atom_cnt == 1  # spared: humus_flag
