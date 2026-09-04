"""Recovery history for task/note edits.

Records a complete snapshot of an entity (``task`` or ``note``) at a
point in time, together with the channel, actor, and editing window
that produced it. Designed to be cheap when the autosave fires
keystroke-by-keystroke from the SPA and still useful as a discrete
recovery point on the other channels:

* **Call-grained channels** (``mcp``, ``api``, ``worker``, ``cli``,
  ``telegram``, ``system``, ``restore``): every ``append`` writes a
  sealed-on-arrival row. One call, one revision.
* **Web channel** (the SPA): coalesces consecutive edits with the
  same ``edit_session_id`` until either the client seals or 30s of
  idle pass. A safety-net job in the worker forces a seal on rows
  open for more than ``IDLE_SAFETY_SEAL_SECONDS``.

Restoring is never an in-place rewrite of a sealed row: it produces a
fresh ``restore``-channel revision with ``restored_from`` set, so the
timeline is monotonic and the restore itself is auditable.

The audit log (``activity_log``) keeps its own role: security/audit,
fine-grained, append-only-mutation-impossible. This module is the
recovery/UX ledger and is intentionally distinct (different lifetime,
different retention, different consumer).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.entity_revision import EntityRevision
from mycelium_core.models.note import Note
from mycelium_core.models.note_tag import NoteTag
from mycelium_core.models.tag import Tag
from mycelium_core.models.task import Task
from mycelium_core.models.task_tag import TaskTag

# Coalescing window: an open web revision absorbs further keystrokes
# only while the last edit lies within this gap. Past the gap, the
# next edit starts a fresh revision (the previous one auto-seals).
COALESCE_WINDOW_SECONDS = 30
# Safety net: the background job in the worker seals any web revision
# that has been open this long, in case the client never sends an
# explicit seal (tab closed, network drop, hard reload).
IDLE_SAFETY_SEAL_SECONDS = 60

ENTITY_KIND_TASK = "task"
ENTITY_KIND_NOTE = "note"
ENTITY_KIND_ANNOTATION = "annotation"
_VALID_KINDS: frozenset[str] = frozenset(
    {ENTITY_KIND_TASK, ENTITY_KIND_NOTE, ENTITY_KIND_ANNOTATION}
)
_VALID_CHANNELS: frozenset[str] = frozenset(
    {"web", "mcp", "api", "worker", "cli", "telegram", "restore", "system"}
)
_COALESCING_CHANNELS: frozenset[str] = frozenset({"web"})


def _json_safe(value: Any) -> Any:
    """Coerce a snapshot value into a JSON-serialisable form.

    Decimal and date/datetime/UUID round-trip as strings so the
    snapshot stays self-contained: a future restore reads the snapshot
    via the same coercion and feeds the right Python type back into
    the update path. ``None`` and primitive scalars pass through.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.UTC).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, list | tuple):
        return [_json_safe(x) for x in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


# Fields that the snapshot captures per entity kind. ``priority`` is
# derived from importance*urgency; the snapshot still records it for
# visual diff readability but restore writes importance/urgency, not
# priority directly (the task update path re-derives it).
_TASK_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "state_id",
    "priority",
    "importance",
    "urgency",
    "start_date",
    "due_date",
    "billable",
    "parent_task_id",
    "estimate_effort_h",
    "required_capabilities",
    "monetary_cost",
    "location",
    "necessity",
    "budget_id",
    # Recorded, never restored: it is in neither RESTORABLE set below,
    # so reverting a task cannot silently put a scoped-out row back into
    # the index. Same treatment as ``is_archived`` / ``deleted_at``.
    "index_scope",
    "is_archived",
    "deleted_at",
    "start_at",
    "duration_minutes",
    "recurrence",
)
# Phase 6 final: ``transcript`` left the Note row in migration 0012.
# It still appears in this tuple for two reasons: (a) the snapshot
# JSON keeps the key (filled from note_part(ord=0)+ joined inside
# ``snapshot_note``) so already-sealed revisions remain restorable,
# and (b) _NOTE_RESTORABLE_FIELDS (below) lists ``transcript`` as a
# field the restore path is allowed to write back. The two sets
# must stay in sync (an anti-drift test pins it).
_NOTE_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "title",
    "transcript",
    "summary",
    "status",
    "maturity",
    "kind",
    "is_archived",
    "deleted_at",
    "audio_ref",
    "audio_seconds",
    "index_scope",
    "promoted_at",
)
# Columns that live on the Note row -- the snapshot loader iterates
# this tuple via ``getattr`` so it never tries to read the dropped
# ``transcript`` attribute. ``snapshot_note`` adds ``transcript``
# explicitly from the parts join.
_NOTE_SNAPSHOT_ROW_FIELDS: tuple[str, ...] = tuple(
    f for f in _NOTE_SNAPSHOT_FIELDS if f != "transcript"
)
# Fields that the restore path is allowed to write back. Identity-,
# routing- and accountability-bearing columns (owner, assignee,
# created_by_*, executor_kind, state_id, status) are deliberately
# OUT: a restore must not silently re-route a task or flip a note's
# pipeline state, only revert the human-edited content.
_TASK_RESTORABLE_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "description",
        "importance",
        "urgency",
        "start_date",
        "due_date",
        "billable",
        "parent_task_id",
        "estimate_effort_h",
        "required_capabilities",
        "monetary_cost",
        "location",
        "necessity",
        "budget_id",
        "start_at",
        "duration_minutes",
        "recurrence",
    }
)
_NOTE_RESTORABLE_FIELDS: frozenset[str] = frozenset({"title", "transcript"})
# A comment/suggestion (migration 0090). ``body`` is the human-written
# text; the rest is context the timeline shows but a restore must not
# rewrite.
_ANNOTATION_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "body",
    "kind",
    "status",
    "doc_kind",
    "anchor_quote",
    "original_text",
    "proposed_text",
    "parent_id",
    "author_identity_id",
    "assigned_to_identity_id",
    "resolved_at",
    "edited_at",
    "deleted_at",
)
# ``body`` ALONE. Restoring a comment must not un-resolve a thread,
# re-assign it, reattach it to a different document or rewrite its
# authorship -- the same principle that keeps ``state_id`` / ``status``
# out of the task and note sets: revert the words, never the routing.
# ``original_text`` / ``proposed_text`` stay out too: a suggestion that
# was accepted spliced those into a document, so rewriting them after
# the fact would desynchronise the suggestion from the edit it made.
_ANNOTATION_RESTORABLE_FIELDS: frozenset[str] = frozenset({"body"})


@dataclasses.dataclass(frozen=True)
class AppendResult:
    """Outcome of ``append``: the row that holds this edit.

    ``coalesced=True`` means the row already existed (open web
    revision) and was updated in place; ``False`` means a new row was
    inserted (any non-web channel, or a fresh web window).
    """

    revision_id: uuid.UUID
    coalesced: bool


async def snapshot_task(session: AsyncSession, task: Task) -> dict[str, Any]:
    """Build a complete snapshot payload from a Task ORM row.

    Includes tags as ``[{id, kind, name, color}]`` so the timeline can
    show tag changes without an extra join at read time.
    """
    payload: dict[str, Any] = {
        field: _json_safe(getattr(task, field)) for field in _TASK_SNAPSHOT_FIELDS
    }
    payload["tags"] = await _tags_for_task(session, task_id=task.id)
    return payload


async def snapshot_note(session: AsyncSession, note: Note) -> dict[str, Any]:
    """Build a complete snapshot payload from a Note ORM row. Phase 6
    final: the canonical body lives in ``note_part(ord=0)+``.

    The body is captured TWICE, on purpose:

    - ``transcript`` -- the flat ``\\n\\n``-joined body. The display
      contract: the SPA revision diff and every existing consumer read
      this key, and every revision written before migration 0089 has
      only this.
    - ``parts`` -- the structural body, one entry per part with its id,
      ord, title, lang, version and body. This is what a restore
      replays, so restoring a multi-part note puts the parts back as
      parts instead of collapsing them into part(ord=0).

    The duplication costs storage proportional to the body on every
    note revision; the retention sweep and ``coarsen`` bound it. The
    alternative -- deriving ``transcript`` at read time -- would move
    the cost onto the SPA diff and the two payload serializers, for a
    field they already treat as stored.
    """
    from mycelium_core.services.note_parts import list_parts as _list_parts
    from mycelium_core.services.notes import get_body as _get_body

    payload: dict[str, Any] = {
        field: _json_safe(getattr(note, field)) for field in _NOTE_SNAPSHOT_ROW_FIELDS
    }
    payload["transcript"] = _json_safe(await _get_body(session, note_id=note.id))
    payload["parts"] = [
        {
            "id": str(p.id),
            "ord": int(p.ord),
            "title": p.title,
            "body": p.body or "",
            "lang": p.lang,
            "version": int(p.version),
        }
        # A snapshot photographs whatever is there, on both axes: the
        # delete revision is written AFTER ``deleted_at`` is set, and a
        # note can be gated for review too. Either gate applied here would
        # snapshot an empty body, and restoring that revision would EMPTY
        # the note (task a186c989).
        for p in await _list_parts(
            session,
            org_id=note.org_id,
            note_id=note.id,
            include_deleted=True,
            include_proposed=True,
        )
    ]
    payload["tags"] = await _tags_for_note(session, note_id=note.id)
    return payload


def snapshot_annotation(annotation: Any) -> dict[str, Any]:
    """Snapshot payload for a comment / suggestion (migration 0090).

    Synchronous and tag-free, unlike the task and note snapshots: an
    annotation owns no junction rows, so there is nothing to join and no
    reason to make every caller await.
    """
    return {
        field: _json_safe(getattr(annotation, field, None)) for field in _ANNOTATION_SNAPSHOT_FIELDS
    }


async def _tags_for_task(session: AsyncSession, *, task_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Tag.id, Tag.kind, Tag.name, Tag.color)
            .join(TaskTag, TaskTag.tag_id == Tag.id)
            .where(TaskTag.task_id == task_id)
        )
    ).all()
    return [
        {
            "id": str(tid),
            "kind": getattr(kind, "value", kind),
            "name": name,
            "color": color,
        }
        for tid, kind, name, color in rows
    ]


async def _tags_for_note(session: AsyncSession, *, note_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Tag.id, Tag.kind, Tag.name, Tag.color)
            .join(NoteTag, NoteTag.tag_id == Tag.id)
            .where(NoteTag.note_id == note_id)
        )
    ).all()
    return [
        {
            "id": str(tid),
            "kind": getattr(kind, "value", kind),
            "name": name,
            "color": color,
        }
        for tid, kind, name, color in rows
    ]


async def _read_actor_context(session: AsyncSession) -> tuple[str, uuid.UUID | None]:
    """Read ``actor_kind`` / ``actor_subject`` from the session GUCs.

    Same source the audit-log helper reads from, so the recovery
    timeline and the audit log stay attributed coherently.
    """
    row = (
        await session.execute(
            text(
                "SELECT current_setting('app.current_actor_kind', true),"
                "       current_setting('app.current_actor_subject', true)"
            )
        )
    ).one()
    actor_kind = row[0] or "human_direct"
    actor_subject_raw = row[1] or ""
    try:
        actor_subject_id = uuid.UUID(actor_subject_raw) if actor_subject_raw else None
    except ValueError:
        actor_subject_id = None
    return actor_kind, actor_subject_id


async def append(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    entity_kind: str,
    entity_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    snapshot: Mapping[str, Any],
    changed_fields: Sequence[str],
    channel: str,
    version_from: int,
    version_to: int,
    edit_session_id: str | None = None,
    restored_from: uuid.UUID | None = None,
) -> AppendResult:
    """Append a revision, coalescing into the open window when allowed.

    On the ``web`` channel with an ``edit_session_id`` an open
    revision matching ``(entity, actor, edit_session_id)`` whose
    ``last_edit_at`` lies within ``COALESCE_WINDOW_SECONDS`` absorbs
    this edit in place. Otherwise (any non-web channel, web without
    a session, or a stale open row) a fresh row is inserted and any
    expired open row for the same editing key is sealed first to
    satisfy the partial unique index.
    """
    if entity_kind not in _VALID_KINDS:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    if channel not in _VALID_CHANNELS:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    if version_to < version_from:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    actor_kind, actor_subject_id = await _read_actor_context(session)
    snapshot_json = {k: _json_safe(v) for k, v in snapshot.items()}
    fields_list = list(dict.fromkeys(changed_fields))
    now = dt.datetime.now(tz=dt.UTC)

    if channel in _COALESCING_CHANNELS and edit_session_id is not None:
        coalesced = await _try_coalesce(
            session,
            entity_kind=entity_kind,
            entity_id=entity_id,
            actor_id=actor_id,
            edit_session_id=edit_session_id,
            channel=channel,
            now=now,
            snapshot_json=snapshot_json,
            fields_list=fields_list,
            version_to=version_to,
        )
        if coalesced is not None:
            return AppendResult(revision_id=coalesced, coalesced=True)
        # Either no open row, or the open row was stale and we sealed
        # it. Fall through to insert a new revision.

    # Insert path. Any expired open row for the same editing key
    # would block the partial unique index, so seal it first.
    await _seal_stale_open(
        session,
        entity_kind=entity_kind,
        entity_id=entity_id,
        channel=channel,
        edit_session_id=edit_session_id,
        actor_id=actor_id,
        now=now,
    )
    seals_immediately = channel not in _COALESCING_CHANNELS or edit_session_id is None
    sealed_at = now if seals_immediately else None
    revision = EntityRevision(
        id=uuid.uuid4(),
        org_id=org_id,
        entity_kind=entity_kind,
        entity_id=entity_id,
        snapshot=snapshot_json,
        changed_fields=fields_list,
        channel=channel,
        actor_id=actor_id,
        actor_kind=actor_kind,
        actor_subject_id=actor_subject_id,
        edit_session_id=edit_session_id,
        version_from=version_from,
        version_to=version_to,
        edit_count=1,
        started_at=now,
        last_edit_at=now,
        sealed_at=sealed_at,
        restored_from=restored_from,
    )
    session.add(revision)
    await session.flush()
    return AppendResult(revision_id=revision.id, coalesced=False)


async def _try_coalesce(
    session: AsyncSession,
    *,
    entity_kind: str,
    entity_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    edit_session_id: str,
    channel: str,
    now: dt.datetime,
    snapshot_json: Mapping[str, Any],
    fields_list: Sequence[str],
    version_to: int,
) -> uuid.UUID | None:
    """Try to coalesce into the open row for this editing key. Returns
    the row id on success; ``None`` when no open row exists or the
    open row is too old (caller falls through to the insert path).

    ``SELECT FOR UPDATE`` so two concurrent appends on the same
    session don't both decide "no open row" and end up inserting
    twice. The partial unique index is a second line of defence.
    """
    stmt = (
        select(EntityRevision)
        .where(
            EntityRevision.entity_kind == entity_kind,
            EntityRevision.entity_id == entity_id,
            EntityRevision.channel == channel,
            EntityRevision.edit_session_id == edit_session_id,
            EntityRevision.actor_id.is_not_distinct_from(actor_id),
            EntityRevision.sealed_at.is_(None),
        )
        .with_for_update()
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    cutoff = now - dt.timedelta(seconds=COALESCE_WINDOW_SECONDS)
    last_edit = row.last_edit_at
    if last_edit.tzinfo is None:
        last_edit = last_edit.replace(tzinfo=dt.UTC)
    if last_edit < cutoff:
        # Stale. Seal it so the new insert can land; trigger blocks
        # further updates once sealed_at is set.
        row.sealed_at = now
        await session.flush()
        return None
    merged = list(dict.fromkeys(list(row.changed_fields or []) + list(fields_list)))
    row.snapshot = dict(snapshot_json)
    row.changed_fields = merged
    row.last_edit_at = now
    row.version_to = version_to
    row.edit_count = (row.edit_count or 1) + 1
    await session.flush()
    return row.id


async def _seal_stale_open(
    session: AsyncSession,
    *,
    entity_kind: str,
    entity_id: uuid.UUID,
    channel: str,
    edit_session_id: str | None,
    actor_id: uuid.UUID | None,
    now: dt.datetime,
) -> None:
    """Force-seal any open row that would collide on the partial
    unique index ``uq_entity_revision_open``. Only the currently-open
    row can be touched: the BEFORE UPDATE trigger blocks updates on
    sealed rows.
    """
    await session.execute(
        update(EntityRevision)
        .where(
            EntityRevision.entity_kind == entity_kind,
            EntityRevision.entity_id == entity_id,
            EntityRevision.channel == channel,
            EntityRevision.sealed_at.is_(None),
            EntityRevision.edit_session_id.is_not_distinct_from(edit_session_id),
            EntityRevision.actor_id.is_not_distinct_from(actor_id),
        )
        .values(sealed_at=now)
    )


async def seal_open(
    session: AsyncSession,
    *,
    entity_kind: str,
    entity_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    edit_session_id: str | None = None,
) -> int:
    """Client-initiated seal: idempotent. Closes any open ``web``
    revision matching the editing key. Returns the count of sealed
    rows (0 when there was nothing to do).
    """
    if entity_kind not in _VALID_KINDS:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    now = dt.datetime.now(tz=dt.UTC)
    stmt = (
        update(EntityRevision)
        .where(
            EntityRevision.entity_kind == entity_kind,
            EntityRevision.entity_id == entity_id,
            EntityRevision.channel == "web",
            EntityRevision.sealed_at.is_(None),
            EntityRevision.actor_id.is_not_distinct_from(actor_id),
        )
        .values(sealed_at=now)
    )
    if edit_session_id is not None:
        stmt = stmt.where(EntityRevision.edit_session_id == edit_session_id)
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0)


async def seal_idle(
    session: AsyncSession, *, older_than_seconds: int = IDLE_SAFETY_SEAL_SECONDS
) -> int:
    """Safety-net seal: closes every open ``web`` revision in the
    current tenant whose ``last_edit_at`` is older than the cutoff.
    Idempotent. Returns the count of rows transitioned.

    Tenant-scoped by RLS: ``entity_revision`` is ``FORCE ROW LEVEL
    SECURITY`` so even admin sessions see zero rows without
    ``app.current_org`` set. The worker enumerates orgs via
    ``admin_session`` and calls this from a ``tenant_session`` per
    org — same pattern as ``task_search_backfill`` /
    ``garden`` / ``reminders``.
    """
    cutoff = dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=older_than_seconds)
    stmt = (
        update(EntityRevision)
        .where(
            EntityRevision.channel == "web",
            EntityRevision.sealed_at.is_(None),
            EntityRevision.last_edit_at < cutoff,
        )
        .values(sealed_at=dt.datetime.now(tz=dt.UTC))
    )
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0)


async def list_revisions(
    session: AsyncSession,
    *,
    entity_kind: str,
    entity_id: uuid.UUID,
    limit: int = 50,
    before: dt.datetime | None = None,
) -> list[EntityRevision]:
    """Page through the timeline, most recent first. ``before`` filters
    on ``COALESCE(sealed_at, last_edit_at)`` so the open revision is
    visible at the head when present.
    """
    if entity_kind not in _VALID_KINDS:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    capped = max(1, min(int(limit), 200))
    order_expr = func.coalesce(EntityRevision.sealed_at, EntityRevision.last_edit_at)
    stmt = (
        select(EntityRevision)
        .where(
            EntityRevision.entity_kind == entity_kind,
            EntityRevision.entity_id == entity_id,
        )
        .order_by(order_expr.desc(), EntityRevision.id.desc())
        .limit(capped)
    )
    if before is not None:
        stmt = stmt.where(order_expr < before)
    return list((await session.execute(stmt)).scalars().all())


async def revision_sequence(
    session: AsyncSession,
    *,
    entity_kind: str,
    entity_id: uuid.UUID,
    only_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """Map each id in ``only_ids`` to its 1-based chronological position
    among ALL of the entity's revisions (1 = the first-ever revision).

    The SPA timeline renders this as ``v{n}``. It is NOT the entity row
    ``version``: a part-level edit bumps the PART's version, not the
    note row's, so a parts-based note's revisions would all read the same
    ``version_to`` (e.g. v1). This counter increments once per revision
    instead. Ranked over every revision (a window function) so the number
    stays correct past the listing page cap, then filtered to the handful
    of ids actually shown.
    """
    if not only_ids:
        return {}
    seq_col = (
        func.row_number()
        .over(order_by=(EntityRevision.started_at.asc(), EntityRevision.id.asc()))
        .label("seq")
    )
    ranked = (
        select(EntityRevision.id.label("id"), seq_col)
        .where(
            EntityRevision.entity_kind == entity_kind,
            EntityRevision.entity_id == entity_id,
        )
        .subquery()
    )
    stmt = select(ranked.c.id, ranked.c.seq).where(ranked.c.id.in_(list(only_ids)))
    return {row_id: int(seq) for row_id, seq in (await session.execute(stmt)).all()}


async def get_revision(
    session: AsyncSession,
    *,
    revision_id: uuid.UUID,
    entity_kind: str | None = None,
    entity_id: uuid.UUID | None = None,
) -> EntityRevision:
    """Single revision lookup. Optional ``entity_kind``/``entity_id``
    constraints let routers verify that the revision belongs to the
    addressed entity (defence in depth: RLS already scopes by org).
    """
    stmt = select(EntityRevision).where(EntityRevision.id == revision_id)
    if entity_kind is not None:
        stmt = stmt.where(EntityRevision.entity_kind == entity_kind)
    if entity_id is not None:
        stmt = stmt.where(EntityRevision.entity_id == entity_id)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError(MessageCode.REVISION_NOT_FOUND)
    return row


# Human-friendly summary label per revision. Truncated to this many
# characters before persisting so a chatty LLM can't blow up a row.
# 200 is generous enough for "renamed task, dropped cost, switched
# project to General" yet still fits on a single SPA line.
SUMMARY_MAX_LEN = 200


async def set_summary(
    session: AsyncSession,
    *,
    revision_id: uuid.UUID,
    summary: str | None,
    entity_kind: str | None = None,
    entity_id: uuid.UUID | None = None,
) -> EntityRevision:
    """Set / clear the ``summary`` label on a revision. The sealed
    immutability trigger has a column allow-list (migration 0010) so
    this UPDATE goes through on sealed rows too. ``None`` clears the
    summary back to its NULL "fallback to changed_fields" default.
    """
    row = await get_revision(
        session,
        revision_id=revision_id,
        entity_kind=entity_kind,
        entity_id=entity_id,
    )
    trimmed: str | None
    if summary is None:
        trimmed = None
    else:
        cleaned = summary.strip()
        if not cleaned:
            trimmed = None
        else:
            trimmed = cleaned[:SUMMARY_MAX_LEN]
    row.summary = trimmed
    await session.flush()
    return row


async def list_pending_summaries(
    session: AsyncSession,
    *,
    limit: int = 50,
) -> list[EntityRevision]:
    """Return sealed revisions whose ``summary`` is still NULL, oldest
    first. Used by the worker sweep to back-fill labels via the LLM in
    chronological order so the timeline becomes "speaking" from the
    earliest gap onwards.
    """
    stmt = (
        select(EntityRevision)
        .where(EntityRevision.sealed_at.is_not(None))
        .where(EntityRevision.summary.is_(None))
        .order_by(EntityRevision.sealed_at.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def erase_for_entity(
    session: AsyncSession,
    *,
    entity_kind: str,
    entity_id: uuid.UUID,
) -> int:
    """Destroy every revision of one entity. Returns the count removed.

    For GDPR erasure only. A snapshot is a FULL COPY of the entity's
    content at a point in time, and this table is polymorphic on
    ``(entity_kind, entity_id)`` with no FK to ``tasks`` / ``notes`` --
    so deleting the entity row leaves its snapshots behind, intact and
    readable. Anything that claims to erase the content has to come
    through here as well, or the text simply moves into the timeline.

    Deliberately NOT wired into the ordinary delete paths: trashing is
    reversible precisely because the history survives it, and the
    retention sweep coarsens by age rather than erasing on demand.
    """
    if entity_kind not in _VALID_KINDS:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    doomed = list(
        (
            await session.execute(
                select(EntityRevision.id).where(
                    EntityRevision.entity_kind == entity_kind,
                    EntityRevision.entity_id == entity_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if doomed:
        # ``restored_from`` is a self-FK ON DELETE SET NULL, so a row
        # outside this set that points at one of these simply loses the
        # pointer instead of blocking the delete.
        await session.execute(
            text("DELETE FROM entity_revision WHERE id = ANY(:ids)"),
            {"ids": [str(i) for i in doomed]},
        )
        await session.flush()
    return len(doomed)


def restorable_payload(
    revision: EntityRevision, *, fields: Sequence[str] | None = None
) -> dict[str, Any]:
    """Project a revision's snapshot onto the fields that the restore
    path is allowed to write back. Identity / routing / lifecycle
    columns are filtered out.

    ``fields=None`` restores every restorable field present in the
    snapshot; a non-empty ``fields`` narrows to that subset and raises
    ``DomainError`` if it contains a non-restorable name.
    """
    allowed = {
        ENTITY_KIND_TASK: _TASK_RESTORABLE_FIELDS,
        ENTITY_KIND_NOTE: _NOTE_RESTORABLE_FIELDS,
        ENTITY_KIND_ANNOTATION: _ANNOTATION_RESTORABLE_FIELDS,
    }[revision.entity_kind]
    snap: Mapping[str, Any] = revision.snapshot or {}
    if fields is None:
        chosen = [f for f in allowed if f in snap]
    else:
        requested = set(fields)
        invalid = requested - allowed
        if invalid:
            raise DomainError(MessageCode.DOMAIN_ERROR)
        chosen = [f for f in requested if f in snap]
    return {f: snap[f] for f in chosen}


async def coarsen(
    session: AsyncSession,
    *,
    retain_full_days: int,
    coarse_to_weekly_days: int,
) -> tuple[int, int]:
    """Retention/coarsening pass for the current tenant. Returns
    ``(deleted_daily, deleted_weekly)``: revisions removed by the
    first and second tier.

    Two tiers, sliding-window over ``COALESCE(sealed_at, last_edit_at)``:

    1. **Daily**: between ``retain_full_days`` and
       ``coarse_to_weekly_days`` ago, keep only the most recent row
       per (entity_kind, entity_id, calendar-day). The bucket is
       ``date_trunc('day', effective_ts)`` so the cut is timezone-
       independent (PG stores UTC).
    2. **Weekly**: older than ``coarse_to_weekly_days``, keep only
       the most recent row per (entity_kind, entity_id,
       calendar-week).

    Idempotent: a row already alone in its bucket survives every
    pass. Never touches rows in the "retain-full" window (everything
    newer than ``retain_full_days``). Never touches the open web
    revision (``sealed_at IS NULL``) -- the safety-net job seals
    those first, retention runs on a slower cadence.
    """
    daily_res = await session.execute(
        text(
            """
            DELETE FROM entity_revision
            WHERE id IN (
              SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                         PARTITION BY entity_kind, entity_id,
                                      date_trunc('day',
                                        COALESCE(sealed_at, last_edit_at))
                         ORDER BY COALESCE(sealed_at, last_edit_at) DESC,
                                  id DESC
                       ) AS rn
                FROM entity_revision
                WHERE sealed_at IS NOT NULL
                  AND COALESCE(sealed_at, last_edit_at)
                        < now() - make_interval(days => :full_days)
                  AND COALESCE(sealed_at, last_edit_at)
                        >= now() - make_interval(days => :weekly_days)
              ) ranked
              WHERE rn > 1
            )
            """
        ),
        {"full_days": retain_full_days, "weekly_days": coarse_to_weekly_days},
    )
    weekly_res = await session.execute(
        text(
            """
            DELETE FROM entity_revision
            WHERE id IN (
              SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                         PARTITION BY entity_kind, entity_id,
                                      date_trunc('week',
                                        COALESCE(sealed_at, last_edit_at))
                         ORDER BY COALESCE(sealed_at, last_edit_at) DESC,
                                  id DESC
                       ) AS rn
                FROM entity_revision
                WHERE sealed_at IS NOT NULL
                  AND COALESCE(sealed_at, last_edit_at)
                        < now() - make_interval(days => :weekly_days)
              ) ranked
              WHERE rn > 1
            )
            """
        ),
        {"weekly_days": coarse_to_weekly_days},
    )
    daily = int(getattr(daily_res, "rowcount", 0) or 0)
    weekly = int(getattr(weekly_res, "rowcount", 0) or 0)
    return daily, weekly


async def hard_delete_soft_deleted(
    session: AsyncSession,
    *,
    after_days: int,
) -> tuple[int, int]:
    """Hard-delete task and note rows whose ``deleted_at`` is older
    than the cutoff. The AFTER DELETE cascade trigger purges their
    revisions. Returns ``(tasks_deleted, notes_deleted)``.

    Runs in the current tenant via RLS, same pattern as ``coarsen``.

    WS-F1 / docs/adr/0041: this is the *autonomous* retention sweep, and
    it must never destroy an "original" the system promised to keep. A
    note that is humus (``humus_flag`` -- fertiliser the ADR-0034 walk
    surfaces) or the SOURCE of a distillation (a ``hypha_of`` parent, so
    it has derived nodes) is spared and stays soft-deleted indefinitely,
    recoverable. Explicit user / GDPR erasure is a separate, sovereign
    path and still cascades -- this guard only gates the timer.

    Task c5da112c: the raw-SQL DELETE bypasses the ORM mapper listeners
    that normally clean the search blobs, so the purged rows' INDEX
    blobs are erased here too. Two deliberate constraints, because this
    is the one AUTONOMOUS hard-delete path (§12):

    - only INDEX provenance is erased -- ``note_part`` pairs and the
      task's ``task_index_pointer`` blob. Whole-entity citation pairs
      (``('note', id)`` / ``('task', id)``) are how independent agent
      memories record where they came from; the timer must never
      destroy those (the sovereign paths -- ``gdpr_erase``,
      ``trash.empty_trash`` -- do cascade them, on an explicit human
      act);
    - the candidate rows are locked with ``FOR UPDATE`` before the part
      ids / pointer ids are collected, so a concurrent transaction
      cannot flip a row's eligibility (restore, humus demote, hypha
      unlink) between the snapshot and the DELETE -- the ids collected
      and the rows deleted are the same set by construction.
    """
    from mycelium_core.services.memory import erase_blobs_for_sources

    task_ids = [
        row[0]
        for row in (
            await session.execute(
                text(
                    """
                    SELECT id FROM tasks
                    WHERE deleted_at IS NOT NULL
                      AND deleted_at < now() - make_interval(days => :d)
                    FOR UPDATE
                    """
                ),
                {"d": after_days},
            )
        ).all()
    ]
    note_ids = [
        row[0]
        for row in (
            await session.execute(
                text(
                    """
                    SELECT n.id FROM notes n
                    WHERE n.deleted_at IS NOT NULL
                      AND n.deleted_at < now() - make_interval(days => :d)
                      AND n.humus_flag = false
                      AND NOT EXISTS (
                        SELECT 1 FROM note_note_link l
                        WHERE l.parent_note_id = n.id AND l.kind = 'hypha_of'
                      )
                    FOR UPDATE OF n
                    """
                ),
                {"d": after_days},
            )
        ).all()
    ]
    if not task_ids and not note_ids:
        return 0, 0
    # Index provenance, gathered while the rows are locked and BEFORE the
    # DELETE cascades destroy note_part / the pointers.
    part_ids: list[uuid.UUID] = []
    if note_ids:
        part_ids = [
            row[0]
            for row in (
                await session.execute(
                    text("SELECT id FROM note_part WHERE note_id = ANY(:ids)"),
                    {"ids": [str(n) for n in note_ids]},
                )
            ).all()
        ]
    task_blob_ids: list[uuid.UUID] = []
    if task_ids:
        task_blob_ids = [
            row[0]
            for row in (
                await session.execute(
                    text("SELECT blob_id FROM task_index_pointer WHERE task_id = ANY(:ids)"),
                    {"ids": [str(t) for t in task_ids]},
                )
            ).all()
        ]
    if task_ids:
        await session.execute(
            text("DELETE FROM tasks WHERE id = ANY(:ids)"),
            {"ids": [str(t) for t in task_ids]},
        )
    if note_ids:
        await session.execute(
            text("DELETE FROM notes WHERE id = ANY(:ids)"),
            {"ids": [str(n) for n in note_ids]},
        )
    await erase_blobs_for_sources(session, sources=[("note_part", str(p)) for p in part_ids])
    if task_blob_ids:
        await session.execute(
            text("DELETE FROM memory_blobs WHERE id = ANY(:ids)"),
            {"ids": [str(b) for b in task_blob_ids]},
        )
    return len(task_ids), len(note_ids)


async def find_visible_open_for(
    session: AsyncSession,
    *,
    entity_kind: str,
    entity_id: uuid.UUID,
) -> EntityRevision | None:
    """The open revision the SPA should badge as ``editing in
    progress``: any open row for this entity, regardless of actor
    (the badge is per-task/note, not per-actor).
    """
    stmt = (
        select(EntityRevision)
        .where(
            EntityRevision.entity_kind == entity_kind,
            EntityRevision.entity_id == entity_id,
            EntityRevision.sealed_at.is_(None),
        )
        .order_by(EntityRevision.last_edit_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


__all__ = (
    "COALESCE_WINDOW_SECONDS",
    "ENTITY_KIND_NOTE",
    "ENTITY_KIND_TASK",
    "IDLE_SAFETY_SEAL_SECONDS",
    "AppendResult",
    "append",
    "coarsen",
    "find_visible_open_for",
    "get_revision",
    "hard_delete_soft_deleted",
    "list_revisions",
    "restorable_payload",
    "revision_sequence",
    "seal_idle",
    "seal_open",
    "snapshot_note",
    "snapshot_task",
)
