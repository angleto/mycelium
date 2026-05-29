"""Note multi-part CRUD + reorder + per-user collapse state.

Phase 2a of the parts cluster (task 71c9d670, parent c0459c4b, design
note 2d228758). Service-layer entry points the REST router + future
MCP tools / CLI dial into. The ``notes.transcript`` column stays as
the source of truth in this phase; the parts table is the new
canonical surface that Phase 6 (task 1cd8bc0a) will promote.

Concurrency: ``NotePart`` carries ``VersionMixin``; mutations use
``optimistic_update`` exactly like ``Note`` / ``Task`` so a SPA
autosave sees the same ``stale_version`` semantics here.

Reorder: the SPA hands us the desired ordering as a list of
``part_ids``; we rewrite every part's ``ord`` to its index in that
list inside a single transaction. The DB-side ``UNIQUE (note_id,
ord) DEFERRABLE INITIALLY DEFERRED`` constraint (migration 0011)
means we never need a 'next free' scratchpad value to step through:
PostgreSQL checks the uniqueness only at COMMIT.

UI state is user-scoped, no version, last-write-wins. The absence of
a row means "expanded" — only an explicit toggle materialises the
row, so the table stays tiny on first visit.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.concurrency import optimistic_update
from flow_core.config import get_settings
from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.membership import Role
from flow_core.models.note import Note
from flow_core.models.note_part import NotePart, NotePartUIState
from flow_core.services import audit
from flow_core.services.rbac import require_role


class _Unset:
    """Sentinel for omit-vs-explicit-None on the patch path. Mirrors
    the pattern in services.notes for symmetric semantics."""


_UNSET: Any = _Unset()


async def list_parts(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_id: uuid.UUID,
) -> list[NotePart]:
    """Return the parts of a note in ``ord`` order. Member-level
    (RLS already scopes the SELECT to the tenant)."""
    rows = (
        (
            await session.execute(
                select(NotePart)
                .where(NotePart.note_id == note_id, NotePart.org_id == org_id)
                .order_by(NotePart.ord, NotePart.id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def parts_by_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, list[NotePart]]:
    """Batched ``{note_id: [parts]}`` for the list endpoints. One
    query, ordered so each note's value is already sorted."""
    if not note_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(NotePart)
                .where(
                    NotePart.org_id == org_id,
                    NotePart.note_id.in_(list(note_ids)),
                )
                .order_by(NotePart.note_id, NotePart.ord, NotePart.id)
            )
        )
        .scalars()
        .all()
    )
    out: dict[uuid.UUID, list[NotePart]] = {}
    for part in rows:
        out.setdefault(part.note_id, []).append(part)
    return out


async def _get_note_in_org(session: AsyncSession, *, org_id: uuid.UUID, note_id: uuid.UUID) -> Note:
    note = (
        await session.execute(select(Note).where(Note.id == note_id, Note.org_id == org_id))
    ).scalar_one_or_none()
    if note is None:
        raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
    return note


async def _get_part(session: AsyncSession, *, org_id: uuid.UUID, part_id: uuid.UUID) -> NotePart:
    part = (
        await session.execute(
            select(NotePart).where(NotePart.id == part_id, NotePart.org_id == org_id)
        )
    ).scalar_one_or_none()
    if part is None:
        raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
    return part


async def get_part(session: AsyncSession, *, org_id: uuid.UUID, part_id: uuid.UUID) -> NotePart:
    """Read a single part by id: random access into a long note's body
    without fetching every other part. Member-level; RLS already scopes
    the SELECT to the tenant. Raises ``NOTE_NOT_FOUND`` for an unknown or
    foreign part id."""
    return await _get_part(session, org_id=org_id, part_id=part_id)


async def create_part(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    body: str,
    title: str | None = None,
    lang: str | None = None,
    ord: int | None = None,
) -> NotePart:
    """Append a new part to the note. When ``ord`` is omitted the
    part lands at the end (max(existing ord) + 1, or 0 if empty);
    when supplied it inserts at that position, pushing every part
    with ord >= ``ord`` forward by one. The push is a single UPDATE
    against the deferred-unique constraint."""
    await require_role(session, org_id, actor_id, Role.member)
    await _get_note_in_org(session, org_id=org_id, note_id=note_id)
    if ord is None:
        max_ord = (
            await session.execute(select(func.max(NotePart.ord)).where(NotePart.note_id == note_id))
        ).scalar()
        target_ord = 0 if max_ord is None else int(max_ord) + 1
    else:
        if ord < 0:
            raise DomainError(MessageCode.DOMAIN_ERROR)
        target_ord = ord
        # Shift everyone at >= target_ord up by one (deferred unique
        # constraint tolerates the transient collision until COMMIT).
        await session.execute(
            NotePart.__table__.update()
            .where(
                NotePart.note_id == note_id,
                NotePart.org_id == org_id,
                NotePart.ord >= target_ord,
            )
            .values(ord=NotePart.ord + 1)
        )
    part = NotePart(
        org_id=org_id,
        note_id=note_id,
        ord=target_ord,
        title=title,
        body=body,
        lang=lang,
    )
    session.add(part)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note_part",
        entity_id=part.id,
        action="create",
        diff={"note_id": str(note_id), "ord": str(target_ord)},
    )
    return part


async def update_part(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    part_id: uuid.UUID,
    expected_version: int,
    body: str | None = None,
    title: str | None | _Unset = _UNSET,
    lang: str | None | _Unset = _UNSET,
    channel: str = "api",
    edit_session_id: str | None = None,
) -> int:
    """Edit a part's ``body`` and/or ``lang``. Returns the new
    version. ``lang`` uses an "omit" sentinel so the caller can
    explicitly clear it (pass None as a JSON null).

    ``channel`` + ``edit_session_id`` flow through to the parent
    note's recovery-history revision so a debounced SPA autosave
    coalesces into a single open revision instead of stamping a
    sealed row per keystroke. The part row's own ``version`` still
    bumps on every save (optimistic_update); only the note-level
    revision row coalesces."""
    await require_role(session, org_id, actor_id, Role.member)
    part = await _get_part(session, org_id=org_id, part_id=part_id)
    values: dict[str, Any] = {}
    if body is not None:
        values["body"] = body
    if not isinstance(title, _Unset):
        values["title"] = title
    if not isinstance(lang, _Unset):
        values["lang"] = lang
    if not values:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    new_version = await optimistic_update(
        session,
        NotePart,
        pk=part_id,
        expected_version=expected_version,
        values=values,
    )
    # Record a note-level revision so the timeline reflects part
    # edits. version_from == version_to: the note's row version is
    # not bumped by part changes (parts carry their own VersionMixin),
    # but the snapshot (which derives ``transcript`` from parts)
    # captures the new body. Lazy import: avoids a hard import cycle
    # between note_parts and notes via entity_revisions.
    from flow_core.services.notes import _log_note_revision

    note = await _get_note_in_org(session, org_id=org_id, note_id=part.note_id)
    await _log_note_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=part.note_id,
        version_from=note.version,
        version_to=note.version,
        changed_fields=[
            "parts.body"
            if "body" in values
            else ("parts.title" if "title" in values else "parts.lang")
        ],
        channel=channel,
        edit_session_id=edit_session_id,
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note_part",
        entity_id=part_id,
        action="update",
        diff={k: str(v) for k, v in values.items()},
    )
    return new_version


async def append_to_part(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    part_id: uuid.UUID,
    chunk: str,
    expected_version: int,
    is_last: bool = True,
    operation_id: str | None = None,
    channel: str = "api",
) -> tuple[int, int]:
    """Append ``chunk`` to a part's body WITHOUT resending the existing
    body. Built for streaming a markdown file past the MCP/JSON-RPC
    per-``tools/call`` payload cap (~100k chars): the client splits the
    file into ordered chunks and appends them in sequence, each asserting
    ``expected_version`` (the cursor). Returns ``(new_version,
    appended_chars)``.

    Chunks are concatenated **raw** (no separator) so the reassembled
    body is byte-for-byte identical to the source file.

    Idempotency (retry-safe, no extra storage): a replay of the same
    chunk at the same cursor -- the part version is exactly one ahead of
    ``expected_version`` AND the body already ends with ``chunk`` -- is a
    no-op returning ``(current_version, 0)``.

    Concurrency: any other version mismatch raises ``stale_version`` (no
    last-write-wins), so two writers racing the same part cannot silently
    interleave.

    History: the note-level recovery revision is logged once, on the
    final chunk (``is_last=True``), so a 16-chunk upload seals a single
    revision capturing the final body rather than 16 growing snapshots.
    ``operation_id`` flows through as the revision's ``edit_session_id``.

    Refuses with ``body.limit_exceeded`` when the resulting body would
    exceed ``FLOW_NOTE_BODY_MAX_BYTES``.
    """
    await require_role(session, org_id, actor_id, Role.member)
    part = await _get_part(session, org_id=org_id, part_id=part_id)
    body = part.body or ""
    # Idempotent replay: cursor advanced by exactly one and the tail is
    # already this chunk -> the previous attempt landed; treat as no-op.
    if part.version == expected_version + 1 and chunk and body.endswith(chunk):
        return part.version, 0
    new_body = body + chunk
    max_bytes = get_settings().note_body_max_bytes
    if len(new_body.encode("utf-8")) > max_bytes:
        raise DomainError(MessageCode.BODY_LIMIT_EXCEEDED, max_bytes=str(max_bytes))
    new_version = await optimistic_update(
        session,
        NotePart,
        pk=part_id,
        expected_version=expected_version,
        values={"body": new_body},
    )
    if is_last:
        # Lazy import: avoids the note_parts <-> notes import cycle.
        from flow_core.services.notes import _log_note_revision

        note = await _get_note_in_org(session, org_id=org_id, note_id=part.note_id)
        await _log_note_revision(
            session,
            org_id=org_id,
            actor_id=actor_id,
            note_id=part.note_id,
            version_from=note.version,
            version_to=note.version,
            changed_fields=["parts.body"],
            channel=channel,
            edit_session_id=operation_id,
        )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note_part",
        entity_id=part_id,
        action="append",
        diff={"appended_chars": str(len(chunk)), "is_last": str(is_last)},
    )
    return new_version, len(chunk)


async def delete_part(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    part_id: uuid.UUID,
) -> None:
    """Hard-delete a part. The remaining parts keep their ords (no
    automatic compaction) so any deep-link via ord survives; reorder
    is an explicit operation."""
    await require_role(session, org_id, actor_id, Role.member)
    part = await _get_part(session, org_id=org_id, part_id=part_id)
    await session.execute(delete(NotePart).where(NotePart.id == part_id, NotePart.org_id == org_id))
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note_part",
        entity_id=part_id,
        action="delete",
        diff={"note_id": str(part.note_id), "ord": str(part.ord)},
    )


async def reorder_parts(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    part_ids: Sequence[uuid.UUID],
) -> list[NotePart]:
    """Rewrite every part's ``ord`` so the sequence matches
    ``part_ids`` exactly. The caller is expected to send the FULL
    ordering (all current parts, in the desired order); a missing or
    extra part raises DOMAIN_ERROR so the SPA can't accidentally
    drop a row by reordering.

    Implementation: bump every targeted row to ``i + 1_000_000`` to
    move them out of the deferred-unique check's collision window
    in one pass, then bring them down to ``i`` in a second pass.
    Two UPDATEs is cheaper than ``len(parts)`` individual ones and
    survives any concurrent write because the deferred constraint
    is the final check at COMMIT.
    """
    await require_role(session, org_id, actor_id, Role.member)
    await _get_note_in_org(session, org_id=org_id, note_id=note_id)
    existing = await list_parts(session, org_id=org_id, note_id=note_id)
    if {p.id for p in existing} != set(part_ids):
        raise DomainError(MessageCode.DOMAIN_ERROR)
    if len(part_ids) != len(set(part_ids)):
        raise DomainError(MessageCode.DOMAIN_ERROR)
    # Two-pass swap. The deferred constraint tolerates collisions
    # mid-transaction; the high-ord pass would still be safe under an
    # IMMEDIATE constraint (no duplicates inside the high range), so
    # the pattern works regardless of when the next COMMIT lands.
    HIGH = 1_000_000
    for i, pid in enumerate(part_ids):
        await session.execute(
            NotePart.__table__.update()
            .where(NotePart.id == pid, NotePart.org_id == org_id)
            .values(ord=HIGH + i)
        )
    for i, pid in enumerate(part_ids):
        await session.execute(
            NotePart.__table__.update()
            .where(NotePart.id == pid, NotePart.org_id == org_id)
            .values(ord=i)
        )
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note_id,
        action="parts_reorder",
        diff={"part_ids": ",".join(str(p) for p in part_ids)},
    )
    return await list_parts(session, org_id=org_id, note_id=note_id)


async def set_ui_state(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    part_id: uuid.UUID,
    collapsed: bool,
) -> NotePartUIState:
    """Toggle the user's collapse state for a part. Upsert on
    (user_id, part_id) — last-write-wins, no version. The part must
    belong to a note in this org (RLS already enforces it, but we
    fetch defensively so a stale part_id surfaces as NOT_FOUND)."""
    await require_role(session, org_id, user_id, Role.member)
    await _get_part(session, org_id=org_id, part_id=part_id)
    stmt = (
        pg_insert(NotePartUIState)
        .values(user_id=user_id, part_id=part_id, collapsed=collapsed)
        .on_conflict_do_update(
            index_elements=[NotePartUIState.user_id, NotePartUIState.part_id],
            set_={"collapsed": collapsed, "updated_at": func.now()},
        )
        .returning(NotePartUIState)
    )
    row = (await session.execute(stmt)).scalar_one()
    return row


async def get_ui_states_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    note_id: uuid.UUID,
) -> dict[uuid.UUID, bool]:
    """``{part_id: collapsed}`` map for the calling user over the
    parts of ``note_id``. Missing entries default to expanded at
    the caller (no row materialisation needed)."""
    rows = (
        await session.execute(
            select(NotePartUIState.part_id, NotePartUIState.collapsed)
            .join(NotePart, NotePart.id == NotePartUIState.part_id)
            .where(
                NotePartUIState.user_id == user_id,
                NotePart.note_id == note_id,
            )
        )
    ).all()
    return {pid: bool(collapsed) for pid, collapsed in rows}


async def merge_notes(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    source_note_id: uuid.UUID,
    target_note_id: uuid.UUID,
    strategy: str = "append",
) -> Note:
    """Fold the source note's parts into the target note (Phase 2b).

    Strategy ``append`` (only one shipped in this PR; ``interleave``
    raises DOMAIN_ERROR until a clear use case asks for it):
    every source part is moved to the target with a fresh ``ord``
    starting at ``max(target.ord) + 1``, ``merged_from_note_id`` is
    set on each moved part so the audit trail survives a future
    source hard-delete, and the source note is soft-deleted. A
    ``supersedes`` NoteNoteLink (target -> source) records the
    relationship so the graph still shows the lineage.

    Idempotency: a second merge of an already-merged source returns
    the target unchanged (the soft-delete check skips it).

    Refuses self-merge and cross-org merges; both raise DOMAIN_ERROR.
    Single transaction (the caller's session.flush() commits the
    move + soft-delete + supersedes link atomically).
    """
    from flow_core.models.note_link import NoteNoteLink

    if strategy != "append":
        raise DomainError(MessageCode.DOMAIN_ERROR)
    if source_note_id == target_note_id:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    await require_role(session, org_id, actor_id, Role.member)
    source = await _get_note_in_org(session, org_id=org_id, note_id=source_note_id)
    target = await _get_note_in_org(session, org_id=org_id, note_id=target_note_id)
    if source.deleted_at is not None:
        # Idempotent: source already merged or deleted earlier.
        return target
    if target.deleted_at is not None:
        raise DomainError(MessageCode.DOMAIN_ERROR)

    source_parts = await list_parts(session, org_id=org_id, note_id=source_note_id)
    target_parts = await list_parts(session, org_id=org_id, note_id=target_note_id)
    next_ord = (target_parts[-1].ord + 1) if target_parts else 0
    # Move each source part to the target: keep the body / lang, reset
    # ``ord`` to land at the tail, stamp ``merged_from_note_id``.
    for offset, sp in enumerate(source_parts):
        await session.execute(
            NotePart.__table__.update()
            .where(NotePart.id == sp.id, NotePart.org_id == org_id)
            .values(
                note_id=target_note_id,
                ord=next_ord + offset,
                merged_from_note_id=source_note_id,
            )
        )
    # Soft-delete the source (matches services.notes.soft_delete_note
    # semantics: deleted_at = now(), maturity untouched, FK rows kept).
    await session.execute(
        Note.__table__.update()
        .where(Note.id == source_note_id, Note.org_id == org_id)
        .values(deleted_at=func.now())
    )
    # Lineage: target supersedes source. Idempotent on the unique
    # (parent, child, kind) triplet.
    existing = (
        await session.execute(
            select(NoteNoteLink).where(
                NoteNoteLink.org_id == org_id,
                NoteNoteLink.parent_note_id == target_note_id,
                NoteNoteLink.child_note_id == source_note_id,
                NoteNoteLink.kind == "supersedes",
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            NoteNoteLink(
                org_id=org_id,
                parent_note_id=target_note_id,
                child_note_id=source_note_id,
                kind="supersedes",
            )
        )
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=target_note_id,
        action="merge_in",
        diff={
            "source_note_id": str(source_note_id),
            "moved_parts": str(len(source_parts)),
        },
    )
    # Refresh the target so the caller sees the post-merge state
    # (e.g. updated_at, version) consistent with what hit the DB.
    return (
        await session.execute(select(Note).where(Note.id == target_note_id, Note.org_id == org_id))
    ).scalar_one()


__all__ = [
    "create_part",
    "delete_part",
    "get_ui_states_for_user",
    "list_parts",
    "merge_notes",
    "parts_by_note",
    "reorder_parts",
    "set_ui_state",
    "update_part",
]
