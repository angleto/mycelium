"""Inline annotations on markdown documents: comments and suggestions.

See ``models/annotation.py`` for the data model. The service boundary
speaks a generic ``(doc_kind, doc_id)`` handle; the typed FK columns and
the XOR CHECK live in the table.

- Authorship is an Identity (ADR-0028). Callers pass
  ``author_identity_id`` explicitly (the MCP layer resolves the
  ai_assistant identity for an agent token); when omitted it defaults to
  the *user* identity of ``actor_id``, so a human author is recorded
  symmetrically.
- A ``comment`` is coordination; a whole-document comment (NULL anchor)
  on a task description is a work-diary entry. A ``suggestion`` carries
  ``original_text`` -> ``proposed_text`` and, on accept, is spliced into
  the live document body (note part or task description); if the target
  text no longer occurs the suggestion goes ``stale`` and is not applied.
- RBAC: ``member`` to read/write; edit/delete require the author (or an
  ``admin``). Optimistic concurrency on every mutation (docs/adr/0002);
  the canonical body stays clean markdown (nothing is written into it
  until a suggestion is accepted).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, cast

from sqlalchemy import ColumnElement, Select, and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.concurrency import optimistic_update
from mycelium_core.config import get_settings
from mycelium_core.errors import DomainError, ForbiddenError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.annotation import (
    ANNOTATION_DOC_KINDS,
    Annotation,
    AnnotationUIState,
)
from mycelium_core.models.identity import Identity, IdentityKind
from mycelium_core.models.membership import Role
from mycelium_core.models.note import Note
from mycelium_core.models.note_part import NotePart
from mycelium_core.models.task import Task
from mycelium_core.services import audit, md_anchor, text_patch
from mycelium_core.services import entity_revisions as _revisions
from mycelium_core.services import identities as identities_svc
from mycelium_core.services.note_effective import effective_note_clause
from mycelium_core.services.rbac import require_role


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
async def _user_identity_id(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID
) -> uuid.UUID | None:
    """The ``user`` Identity of ``actor_id`` in this org, or None."""
    return (
        await session.execute(
            select(Identity.id).where(
                Identity.org_id == org_id,
                Identity.user_id == actor_id,
                Identity.kind == IdentityKind.user,
            )
        )
    ).scalar_one_or_none()


async def _resolve_author(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    author_identity_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Explicit identity (e.g. the ai_assistant resolved by the MCP layer)
    wins; otherwise default to the actor's user identity."""
    if author_identity_id is not None:
        return author_identity_id
    return await _user_identity_id(session, org_id=org_id, actor_id=actor_id)


def _effective_note_anchor() -> ColumnElement[bool]:
    """An annotation is reachable only while the document it hangs on is:
    for a ``note_part`` anchor, that is the note's own perimeter (task
    a186c989).

    An annotation is not just commentary about the text, it QUOTES it:
    ``anchor_quote`` / ``anchor_prefix`` / ``anchor_suffix`` are verbatim
    extracts (W3C TextQuoteSelector) and a suggestion's ``original_text``
    is the exact passage it would replace. So a comment thread on a note
    that went to the bin, or on a proposal awaiting review, hands out the
    body the note surfaces refuse -- two hops away, and with the part id
    that no listing gives out any more.

    Use it with the outer joins below: a ``task_description`` annotation
    has no note in the picture and must pass untouched, and the archive
    is not part of the predicate, so an archived note keeps its threads.
    """
    return or_(Annotation.note_part_id.is_(None), effective_note_clause())


def _with_note_perimeter(stmt: Select[Any]) -> Select[Any]:
    """OUTER join the anchor chain (annotation -> note_part -> note) and
    apply :func:`_effective_note_anchor`. Outer, never inner: an inner
    join would silently drop every task-description annotation, which is
    the whole work diary of every task."""
    return (
        stmt.outerjoin(NotePart, NotePart.id == Annotation.note_part_id)
        .outerjoin(Note, Note.id == NotePart.note_id)
        .where(_effective_note_anchor())
    )


async def _resolve_doc(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    doc_kind: str,
    doc_id: uuid.UUID,
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Validate the markdown-document handle and translate it to the
    typed columns ``(task_id, note_part_id)``. RLS already scopes the
    SELECT to the tenant; the presence check turns a foreign/unknown id
    into a clean 404."""
    if doc_kind not in ANNOTATION_DOC_KINDS:
        raise DomainError(MessageCode.ANNOTATION_DOC_KIND_INVALID)
    if doc_kind == "task_description":
        found = (
            await session.execute(select(Task.id).where(Task.id == doc_id))
        ).scalar_one_or_none()
        if found is None:
            raise NotFoundError(MessageCode.TASK_NOT_FOUND)
        return doc_id, None
    # note_part: and the note it belongs to has to be effective, or the
    # write door would be wider than every read door on the same text.
    found = (
        await session.execute(
            select(NotePart.id)
            .join(Note, Note.id == NotePart.note_id)
            .where(NotePart.id == doc_id, effective_note_clause())
        )
    ).scalar_one_or_none()
    if found is None:
        raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
    return None, doc_id


async def _get(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    annotation_id: uuid.UUID,
    include_deleted: bool = False,
    include_ineffective_note: bool = False,
) -> Annotation:
    """The id-addressed chokepoint of this module, and therefore the place
    the note perimeter belongs: it covers the read, the whole body family,
    the raw-body capability (checked at read time), assign/resolve/restore
    and the accept path.

    ``include_ineffective_note`` is asked for by exactly two callers, and
    for opposite reasons: ``_log_annotation_revision``, which photographs
    whatever state its row is in (a gate inside a logger fails after the
    write, not at the door), and the admin-only ``purge``, because
    withholding a thread from reading must not make it unerasable.
    Everything else stays on the perimeter: a thread on a note in the bin
    is reached by restoring the note, the same way its parts are.
    """
    stmt = select(Annotation)
    if not include_ineffective_note:
        stmt = _with_note_perimeter(stmt)
    ann: Annotation | None = (
        await session.execute(stmt.where(Annotation.id == annotation_id))
    ).scalar_one_or_none()
    if ann is None:
        # ANNOTATION_NOT_FOUND either way: the caller addressed an
        # annotation, and must not learn from the error code whether the
        # id exists behind a gate it may not pass.
        raise NotFoundError(MessageCode.ANNOTATION_NOT_FOUND)
    if ann.deleted_at is not None and not include_deleted:
        raise NotFoundError(MessageCode.ANNOTATION_NOT_FOUND)
    return ann


async def _log_annotation_revision(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    annotation_id: uuid.UUID,
    version_from: int,
    version_to: int,
    changed_fields: list[str],
    channel: str = "api",
    restored_from: uuid.UUID | None = None,
) -> None:
    """Recovery-history entry for a comment mutation (migration 0090).

    Reads the row back so the snapshot reflects the POST-update state --
    the Core UPDATE inside ``optimistic_update`` bypasses the ORM
    mapper, exactly as it does for notes and tasks.

    Channel ``api``, never ``web``: a comment is written in one shot from
    a card, not keystroke-by-keystroke into an autosave window, so there
    is nothing to coalesce and every edit seals its own row.
    """
    # Photographer: record the row whatever state it (or the note it hangs
    # on) is in. A gate here would fail after the write, not at the door.
    fresh = await _get(
        session,
        org_id=org_id,
        annotation_id=annotation_id,
        include_deleted=True,
        include_ineffective_note=True,
    )
    await _revisions.append(
        session,
        org_id=org_id,
        entity_kind=_revisions.ENTITY_KIND_ANNOTATION,
        entity_id=annotation_id,
        actor_id=actor_id,
        snapshot=_revisions.snapshot_annotation(fresh),
        changed_fields=changed_fields,
        channel=channel,
        version_from=version_from,
        version_to=version_to,
        edit_session_id=None,
        restored_from=restored_from,
    )


async def _require_author_or_admin(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    actor_identity_id: uuid.UUID | None,
    ann: Annotation,
) -> None:
    """Edit/delete is the author's right; an admin can override. The
    actor's identity is the explicit one (an agent's ai_assistant
    identity, passed by the MCP layer) or the actor's user identity."""
    ident = actor_identity_id or await _user_identity_id(session, org_id=org_id, actor_id=actor_id)
    if ann.author_identity_id is not None and ann.author_identity_id == ident:
        return
    await require_role(session, org_id, actor_id, Role.admin)


def _splice(
    body: str,
    *,
    original: str,
    proposed: str,
    prefix: str | None,
    suffix: str | None,
    domain: str = "source",
) -> str | None:
    """Apply ``original -> proposed`` to the markdown ``body`` at the anchor
    pinned by ``original`` + optional prefix/suffix. Returns the new body, or
    None when the anchor can no longer be located faithfully (the suggestion
    is stale).

    ``domain`` says which projection the anchor triple is written in.

    ``source`` is what everything captured since the markdown editor's
    document became the markdown itself. A selection IS a source span, so
    locating is ``str.find`` on the body and no projection is involved. The
    splice is then guarded by a BLOCK-SHAPE check, which is what stands in
    for the old path's re-render equality: an agent can propose any string
    through MCP, and one that changes the document's block structure corrupts
    it even though it applies cleanly (a table row gaining a cell, a list item
    splitting, a fence truncated, a paragraph promoted to a heading).

    ``rendered`` is the legacy projection: markdown stripped, links reduced to
    their label, blocks joined by a space, resolved through md_anchor's
    per-character source map. Migration 0099 converted every row it could;
    what is left here could not be located in its own domain either, so this
    branch overwhelmingly declines. It stays because relabelling those rows
    would let the source locator read a quote in a domain it was not written
    in, and that turns a visibly stale anchor into one that may match the
    WRONG passage.

    No persisted offsets in either domain: the anchor is re-located against
    the live body every time, so it survives prior edits and goes stale only
    when its quote is no longer uniquely there."""
    if domain == "rendered":
        return md_anchor.splice(
            body, original=original, proposed=proposed, prefix=prefix, suffix=suffix
        )
    return md_anchor.splice_source(
        body, original=original, proposed=proposed, prefix=prefix, suffix=suffix
    )


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------
async def create_comment(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    doc_kind: str,
    doc_id: uuid.UUID,
    body: str,
    anchor_quote: str | None = None,
    anchor_prefix: str | None = None,
    anchor_suffix: str | None = None,
    anchor_domain: str = "source",
    parent_id: uuid.UUID | None = None,
    author_identity_id: uuid.UUID | None = None,
) -> Annotation:
    """A comment on a markdown document. A NULL ``anchor_quote`` is a
    whole-document comment (on a task description: a work-diary entry).
    A ``parent_id`` makes it a reply (it inherits the parent's document)."""
    await require_role(session, org_id, actor_id, Role.member)
    if parent_id is not None:
        parent = await _get(session, org_id=org_id, annotation_id=parent_id)
        task_id, note_part_id = parent.task_id, parent.note_part_id
        doc_kind = parent.doc_kind
    else:
        task_id, note_part_id = await _resolve_doc(
            session, org_id=org_id, doc_kind=doc_kind, doc_id=doc_id
        )
    author = await _resolve_author(
        session, org_id=org_id, actor_id=actor_id, author_identity_id=author_identity_id
    )
    ann = Annotation(
        org_id=org_id,
        doc_kind=doc_kind,
        task_id=task_id,
        note_part_id=note_part_id,
        kind="comment",
        body=body,
        anchor_quote=anchor_quote,
        anchor_prefix=anchor_prefix,
        anchor_suffix=anchor_suffix,
        anchor_domain=anchor_domain,
        parent_id=parent_id,
        status="open",
        author_identity_id=author,
    )
    session.add(ann)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="annotation",
        entity_id=ann.id,
        action="create",
        diff={"kind": "comment", "doc_kind": doc_kind},
    )
    await _log_annotation_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        annotation_id=ann.id,
        version_from=ann.version,
        version_to=ann.version,
        changed_fields=["_create"],
    )
    return ann


async def propose_suggestion(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    doc_kind: str,
    doc_id: uuid.UUID,
    original_text: str,
    proposed_text: str,
    rationale: str = "",
    anchor_prefix: str | None = None,
    anchor_suffix: str | None = None,
    anchor_domain: str = "source",
    author_identity_id: uuid.UUID | None = None,
) -> Annotation:
    """A proposed edit (``original_text`` -> ``proposed_text``) on a
    markdown document, with an optional rationale (stored in ``body``).
    The struck original is also the anchor quote, so the SPA can paint it
    and accept can relocate it. Nothing touches the document body until
    the suggestion is accepted."""
    await require_role(session, org_id, actor_id, Role.member)
    if not original_text:
        raise DomainError(MessageCode.SUGGESTION_TEXT_REQUIRED)
    task_id, note_part_id = await _resolve_doc(
        session, org_id=org_id, doc_kind=doc_kind, doc_id=doc_id
    )
    author = await _resolve_author(
        session, org_id=org_id, actor_id=actor_id, author_identity_id=author_identity_id
    )
    ann = Annotation(
        org_id=org_id,
        doc_kind=doc_kind,
        task_id=task_id,
        note_part_id=note_part_id,
        kind="suggestion",
        body=rationale,
        anchor_quote=original_text,
        anchor_prefix=anchor_prefix,
        anchor_suffix=anchor_suffix,
        anchor_domain=anchor_domain,
        original_text=original_text,
        # NOT stripped. The strip was a repair for the rendered domain: edge
        # whitespace carried no meaning there (the renderer dropped it) and an
        # un-stripped value made the accept-time re-render gate asymmetric,
        # STALEing an otherwise valid suggestion. In the source domain edge
        # whitespace IS markdown -- a two-space hard break, the indentation of
        # a nested list item, the leading spaces of an indented code block --
        # and the gate it was compensating for no longer runs. Stripping here
        # would silently rewrite the author's proposal.
        proposed_text=proposed_text,
        status="open",
        author_identity_id=author,
    )
    session.add(ann)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="annotation",
        entity_id=ann.id,
        action="create",
        diff={"kind": "suggestion", "doc_kind": doc_kind},
    )
    await _log_annotation_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        annotation_id=ann.id,
        version_from=ann.version,
        version_to=ann.version,
        changed_fields=["_create"],
    )
    return ann


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------
async def list_for_doc(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    doc_kind: str,
    doc_id: uuid.UUID,
    include_resolved: bool = True,
    include_deleted: bool = False,
    kind: str | None = None,
    limit: int | None = None,
    after: tuple[dt.datetime, uuid.UUID] | None = None,
) -> list[Annotation]:
    """Every annotation on a markdown document, oldest first (created_at asc,
    id asc -- a total order, so a task's general comments read as a
    chronological work diary). ``kind`` optionally narrows to ``comment`` or
    ``suggestion``. ``limit`` + the ``after`` keyset cursor page the thread."""
    if doc_kind not in ANNOTATION_DOC_KINDS:
        raise DomainError(MessageCode.ANNOTATION_DOC_KIND_INVALID)
    stmt = _with_note_perimeter(select(Annotation)).where(Annotation.doc_kind == doc_kind)
    if doc_kind == "task_description":
        stmt = stmt.where(Annotation.task_id == doc_id)
    else:
        stmt = stmt.where(Annotation.note_part_id == doc_id)
    if not include_deleted:
        stmt = stmt.where(Annotation.deleted_at.is_(None))
    if not include_resolved:
        stmt = stmt.where(Annotation.status == "open")
    if kind is not None:
        stmt = stmt.where(Annotation.kind == kind)
    if after is not None:
        ac, ai = after
        stmt = stmt.where(
            or_(Annotation.created_at > ac, and_(Annotation.created_at == ac, Annotation.id > ai))
        )
    stmt = stmt.order_by(Annotation.created_at.asc(), Annotation.id.asc())
    if limit is not None and limit > 0:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def count_for_doc(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    doc_kind: str,
    doc_id: uuid.UUID,
    kind: str | None = None,
) -> tuple[int, int]:
    """``(total, open)`` annotation counts on a document via ``COUNT`` queries
    (no row fetch). Excludes soft-deleted; ``kind`` optionally narrows to
    ``comment`` | ``suggestion``. ``total`` counts every non-deleted row,
    ``open`` only those still in ``status='open'`` (the actionable subset)."""
    if doc_kind not in ANNOTATION_DOC_KINDS:
        raise DomainError(MessageCode.ANNOTATION_DOC_KIND_INVALID)
    anchor = Annotation.task_id if doc_kind == "task_description" else Annotation.note_part_id
    base = _with_note_perimeter(select(func.count()).select_from(Annotation)).where(
        Annotation.doc_kind == doc_kind,
        anchor == doc_id,
        Annotation.deleted_at.is_(None),
    )
    if kind is not None:
        base = base.where(Annotation.kind == kind)
    total = int((await session.execute(base)).scalar_one())
    open_ = int((await session.execute(base.where(Annotation.status == "open"))).scalar_one())
    return total, open_


async def get_annotation(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    annotation_id: uuid.UUID,
    include_deleted: bool = False,
) -> Annotation:
    """``include_deleted=True`` reads a soft-deleted row: the only way to
    learn the ``version`` :func:`restore` needs, for a caller that was not
    the one who deleted it. It relaxes the ANNOTATION's own soft-delete and
    nothing else -- the perimeter of the note a comment hangs on (task
    a186c989) has no opt-out here, so a thread on a gated note is out of
    reach until the note itself comes back."""
    return await _get(
        session,
        org_id=org_id,
        annotation_id=annotation_id,
        include_deleted=include_deleted,
    )


async def list_assigned(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    assignee_identity_id: uuid.UUID,
    include_resolved: bool = False,
    limit: int = 100,
) -> list[Annotation]:
    """The "assigned to me" inbox: annotations assigned to
    ``assignee_identity_id`` across the workspace, newest first. Excludes
    soft-deleted; ``include_resolved=False`` keeps only ``open`` items (the
    actionable inbox). RLS scopes the SELECT to the tenant."""
    # The inbox has no document handle to start from, so it needs the
    # perimeter of its own: it was the surface handing back the part id of
    # a gated note to whoever had an assignment on it.
    stmt = _with_note_perimeter(select(Annotation)).where(
        Annotation.assigned_to_identity_id == assignee_identity_id,
        Annotation.deleted_at.is_(None),
    )
    if not include_resolved:
        stmt = stmt.where(Annotation.status == "open")
    stmt = stmt.order_by(Annotation.created_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


# --------------------------------------------------------------------------
# mutate
# --------------------------------------------------------------------------
async def edit(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    annotation_id: uuid.UUID,
    body: str,
    expected_version: int,
    actor_identity_id: uuid.UUID | None = None,
) -> int:
    """Edit the body (comment text or suggestion rationale). Author or
    admin only; stamps ``edited_at``."""
    await require_role(session, org_id, actor_id, Role.member)
    ann = await _get(session, org_id=org_id, annotation_id=annotation_id)
    await _require_author_or_admin(
        session, org_id=org_id, actor_id=actor_id, actor_identity_id=actor_identity_id, ann=ann
    )
    new_version = await optimistic_update(
        session,
        Annotation,
        pk=annotation_id,
        expected_version=expected_version,
        values={"body": body, "edited_at": dt.datetime.now(dt.UTC)},
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="annotation",
        entity_id=annotation_id,
        action="edit",
    )
    await _log_annotation_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        annotation_id=annotation_id,
        version_from=expected_version,
        version_to=new_version,
        changed_fields=["body"],
    )
    return new_version


async def apply_patch_to_body(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    annotation_id: uuid.UUID,
    expected_version: int,
    patch: str,
    base_sha256: str,
    actor_identity_id: uuid.UUID | None = None,
) -> int:
    """Apply a strict unified diff to a comment/annotation body and persist
    via :func:`edit` (author-or-admin gate, ``edited_at`` stamp). Symmetric
    to the note-part and task-description patch helpers: sha256 base gate
    (409 PATCH_STALE) plus the version gate in ``edit``'s
    ``optimistic_update``, one transaction."""
    ann = await _get(session, org_id=org_id, annotation_id=annotation_id)
    new_body = text_patch.apply_patch_text(
        ann.body or "",
        patch,
        expected_sha256=base_sha256,
        max_result_bytes=get_settings().note_body_max_bytes,
    )
    return await edit(
        session,
        org_id=org_id,
        actor_id=actor_id,
        annotation_id=annotation_id,
        body=new_body,
        expected_version=expected_version,
        actor_identity_id=actor_identity_id,
    )


async def append_to_body(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    annotation_id: uuid.UUID,
    text: str,
    separator: str = "\n\n",
    expected_version: int | None = None,
    dedupe_if_tail_matches: bool = False,
    actor_identity_id: uuid.UUID | None = None,
) -> tuple[int, int]:
    """Append ``text`` to a comment/annotation body WITHOUT reading it
    first -- the annotation twin of ``tasks.append_to_description`` and
    ``note_parts.append_to_part``, the one family that had no way to add
    to a body except resending the whole thing.

    ``expected_version=None`` appends onto the current version (the
    blind-append contract the task twin uses).
    ``dedupe_if_tail_matches=True`` makes a replay a no-op returning
    ``(current_version, 0)``. ``BODY_LIMIT_EXCEEDED`` past the body cap.
    Persists through :func:`edit`, inheriting the author-or-admin gate
    and the ``edited_at`` stamp.
    """
    from mycelium_core.services.notes import _collapsed_concat

    ann = await _get(session, org_id=org_id, annotation_id=annotation_id)
    current = ann.body or ""
    if dedupe_if_tail_matches and current and current.rstrip().endswith(text.rstrip()):
        return ann.version, 0
    new_body = _collapsed_concat(current, separator, text)
    max_bytes = get_settings().note_body_max_bytes
    if len(new_body.encode("utf-8")) > max_bytes:
        raise DomainError(MessageCode.BODY_LIMIT_EXCEEDED, max_bytes=str(max_bytes))
    new_version = await edit(
        session,
        org_id=org_id,
        actor_id=actor_id,
        annotation_id=annotation_id,
        body=new_body,
        expected_version=expected_version if expected_version is not None else ann.version,
        actor_identity_id=actor_identity_id,
    )
    return new_version, len(text)


async def prepend_to_body(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    annotation_id: uuid.UUID,
    text: str,
    separator: str = "\n\n",
    expected_version: int | None = None,
    dedupe_if_head_matches: bool = False,
    actor_identity_id: uuid.UUID | None = None,
) -> tuple[int, int]:
    """Prepend ``text`` to the FRONT of a comment/annotation body: the
    mirror of :func:`append_to_body`, same contract with the concat
    order swapped. ``dedupe_if_head_matches=True`` no-ops when the body
    already starts with ``text``."""
    from mycelium_core.services.notes import _collapsed_concat

    ann = await _get(session, org_id=org_id, annotation_id=annotation_id)
    current = ann.body or ""
    if dedupe_if_head_matches and current and current.lstrip().startswith(text.lstrip()):
        return ann.version, 0
    new_body = _collapsed_concat(text, separator, current)
    max_bytes = get_settings().note_body_max_bytes
    if len(new_body.encode("utf-8")) > max_bytes:
        raise DomainError(MessageCode.BODY_LIMIT_EXCEEDED, max_bytes=str(max_bytes))
    new_version = await edit(
        session,
        org_id=org_id,
        actor_id=actor_id,
        annotation_id=annotation_id,
        body=new_body,
        expected_version=expected_version if expected_version is not None else ann.version,
        actor_identity_id=actor_identity_id,
    )
    return new_version, len(text)


async def replace_in_body(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    annotation_id: uuid.UUID,
    find: str,
    replace: str,
    expected_version: int,
    count: int = 0,
    actor_identity_id: uuid.UUID | None = None,
) -> tuple[int, int]:
    """Anchored find/replace inside ONE comment/annotation body without
    resending it: the twin of ``note_parts.replace_in_part`` for the
    annotation family, which had no anchored edit at any scope. Swaps
    the literal ``find`` for ``replace``; ``count`` <= 0 replaces every
    occurrence, a positive ``count`` only the first N. Returns
    ``(new_version, replacements)``.

    A no-op -- ``find`` empty or absent from the body -- returns
    ``(current_version, 0)`` WITHOUT bumping the version and without
    asserting ``expected_version`` (nothing changed, so nothing to
    race), exactly like the note-part twin. Refuses with
    ``body.limit_exceeded`` when the result would outgrow
    ``MYCELIUM_NOTE_BODY_MAX_BYTES``.

    Persists through :func:`edit`, so the author-or-admin gate, the
    ``edited_at`` stamp and the version guard are inherited rather than
    re-implemented (the delegation :func:`apply_patch_to_body` uses).
    """
    ann = await _get(session, org_id=org_id, annotation_id=annotation_id)
    body = ann.body or ""
    occurrences = body.count(find) if find else 0
    if occurrences == 0:
        return ann.version, 0
    n = occurrences if count <= 0 else min(count, occurrences)
    new_body = body.replace(find, replace) if count <= 0 else body.replace(find, replace, count)
    max_bytes = get_settings().note_body_max_bytes
    if len(new_body.encode("utf-8")) > max_bytes:
        raise DomainError(MessageCode.BODY_LIMIT_EXCEEDED, max_bytes=str(max_bytes))
    new_version = await edit(
        session,
        org_id=org_id,
        actor_id=actor_id,
        annotation_id=annotation_id,
        body=new_body,
        expected_version=expected_version,
        actor_identity_id=actor_identity_id,
    )
    return new_version, n


async def assign(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    annotation_id: uuid.UUID,
    expected_version: int,
    assignee_identity_id: uuid.UUID | None = None,
    assignee_handle: str | None = None,
    clear: bool = False,
) -> int:
    """Assign the annotation to a workspace identity (the person responsible
    for acting on it) or clear it (``clear=True``).

    Assigning is *coordination*, not authorship, so ANY member may do it --
    unlike ``edit`` / ``soft_delete`` (author-or-admin). ``assignee_handle``
    resolves via ``identities.lookup_by_handle`` (bare handle, ``@handle``, or
    login email); a passed ``assignee_identity_id`` is validated to belong to
    this org. An unresolved/foreign identity raises ``IDENTITY_NOT_FOUND``.
    Optimistic-versioned + audited like every other annotation mutation."""
    await require_role(session, org_id, actor_id, Role.member)
    await _get(session, org_id=org_id, annotation_id=annotation_id)
    target: uuid.UUID | None
    if clear:
        target = None
    elif assignee_identity_id is not None:
        found = (
            await session.execute(
                select(Identity.id).where(
                    Identity.id == assignee_identity_id, Identity.org_id == org_id
                )
            )
        ).scalar_one_or_none()
        if found is None:
            raise NotFoundError(MessageCode.IDENTITY_NOT_FOUND)
        target = assignee_identity_id
    elif assignee_handle:
        ident = await identities_svc.lookup_by_handle(
            session, org_id=org_id, handle=assignee_handle
        )
        if ident is None:
            raise NotFoundError(MessageCode.IDENTITY_NOT_FOUND)
        target = ident.id
    else:
        # No target and no explicit clear: nothing to do.
        raise DomainError(MessageCode.DOMAIN_ERROR)
    new_version = await optimistic_update(
        session,
        Annotation,
        pk=annotation_id,
        expected_version=expected_version,
        values={"assigned_to_identity_id": target},
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="annotation",
        entity_id=annotation_id,
        action="unassign" if target is None else "assign",
    )
    await _log_annotation_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        annotation_id=annotation_id,
        version_from=expected_version,
        version_to=new_version,
        changed_fields=["assigned_to_identity_id"],
    )
    return new_version


async def soft_delete(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    annotation_id: uuid.UUID,
    expected_version: int,
    actor_identity_id: uuid.UUID | None = None,
) -> int:
    """Soft-delete (the diary/history is retained). For a pending
    suggestion this is the *withdraw*. Author or admin only."""
    await require_role(session, org_id, actor_id, Role.member)
    ann = await _get(session, org_id=org_id, annotation_id=annotation_id)
    await _require_author_or_admin(
        session, org_id=org_id, actor_id=actor_id, actor_identity_id=actor_identity_id, ann=ann
    )
    new_version = await optimistic_update(
        session,
        Annotation,
        pk=annotation_id,
        expected_version=expected_version,
        values={"deleted_at": dt.datetime.now(dt.UTC)},
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="annotation",
        entity_id=annotation_id,
        action="delete",
    )
    await _log_annotation_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        annotation_id=annotation_id,
        version_from=expected_version,
        version_to=new_version,
        changed_fields=["_delete"],
    )
    return new_version


async def restore(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    annotation_id: uuid.UUID,
    expected_version: int,
    actor_identity_id: uuid.UUID | None = None,
) -> int:
    """Undo a :func:`soft_delete`: clear ``deleted_at`` and the comment
    (or withdrawn suggestion) is back in the diary. Author or admin
    only, same gate as the delete it reverses.

    Until this existed ``soft_delete`` had no inverse anywhere in the
    tree: the row was retained but unreachable on every surface, which
    made "soft" delete a one-way door in practice.
    """
    await require_role(session, org_id, actor_id, Role.member)
    ann = await _get(session, org_id=org_id, annotation_id=annotation_id, include_deleted=True)
    await _require_author_or_admin(
        session, org_id=org_id, actor_id=actor_id, actor_identity_id=actor_identity_id, ann=ann
    )
    new_version = await optimistic_update(
        session,
        Annotation,
        pk=annotation_id,
        expected_version=expected_version,
        values={"deleted_at": None},
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="annotation",
        entity_id=annotation_id,
        action="restore",
    )
    await _log_annotation_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        annotation_id=annotation_id,
        version_from=expected_version,
        version_to=new_version,
        changed_fields=["_restore"],
    )
    return new_version


async def restore_revision(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    annotation_id: uuid.UUID,
    revision_id: uuid.UUID,
    expected_version: int,
    actor_identity_id: uuid.UUID | None = None,
) -> int:
    """Revert a comment's ``body`` to the snapshot in ``revision_id``.

    ``body`` is the only restorable field (see
    ``_ANNOTATION_RESTORABLE_FIELDS``): a restore reverts the words, not
    the thread's status, its assignee or its anchoring. Produces a NEW
    revision on the ``restore`` channel with ``restored_from`` set, so
    the timeline stays monotonic and the restore is itself auditable --
    same contract as the task and note restores.
    """
    await require_role(session, org_id, actor_id, Role.member)
    ann = await _get(session, org_id=org_id, annotation_id=annotation_id, include_deleted=True)
    await _require_author_or_admin(
        session, org_id=org_id, actor_id=actor_id, actor_identity_id=actor_identity_id, ann=ann
    )
    revision = await _revisions.get_revision(
        session,
        revision_id=revision_id,
        entity_kind=_revisions.ENTITY_KIND_ANNOTATION,
        entity_id=annotation_id,
    )
    payload = _revisions.restorable_payload(revision)
    if not payload:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    new_version = await optimistic_update(
        session,
        Annotation,
        pk=annotation_id,
        expected_version=expected_version,
        values={"body": payload["body"] or "", "edited_at": dt.datetime.now(dt.UTC)},
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="annotation",
        entity_id=annotation_id,
        action="restore_revision",
    )
    await _log_annotation_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        annotation_id=annotation_id,
        version_from=expected_version,
        version_to=new_version,
        changed_fields=["body"],
        channel="restore",
        restored_from=revision_id,
    )
    return new_version


async def purge(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    annotation_id: uuid.UUID,
) -> None:
    """Destroy a comment for good: the row, its per-user card state and
    its whole revision history. Irreversible -- this is what the
    ``delete:comments`` danger key fences, and the counterpart of
    ``note_parts.delete_part``. The ordinary, restorable removal is
    :func:`soft_delete`.

    Admin only, NOT author-or-admin. Everything else in this module lets
    an author manage their own words, and that is right for a reversible
    delete; erasing a signed entry from a shared conversation for good is
    a different act, and the person best placed to abuse it is its
    author. Accepts a live or an already-soft-deleted row, and one whose
    note is gated: withholding a thread from READING must never mean an
    admin can no longer erase it (task a186c989). That is the same call
    as ``memory.delete_blob``, and the opposite one from
    ``note_parts.delete_part`` -- purging a part of a binned note would
    destroy text that note's own restore needs, while purging a comment
    destroys only the comment.

    The revision history goes with it via ``trg_comment_revision_cascade``
    (migration 0090), which is also the only thing preventing a purged
    comment's text from surviving in the timeline.
    """
    await require_role(session, org_id, actor_id, Role.admin)
    ann = await _get(
        session,
        org_id=org_id,
        annotation_id=annotation_id,
        include_deleted=True,
        include_ineffective_note=True,
    )
    doc_kind = ann.doc_kind
    await session.execute(delete(Annotation).where(Annotation.id == annotation_id))
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="annotation",
        entity_id=annotation_id,
        action="purge",
        diff={"doc_kind": doc_kind},
    )


async def resolve(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    annotation_id: uuid.UUID,
    expected_version: int,
    resolved_by_identity_id: uuid.UUID | None = None,
) -> int:
    """Mark a comment thread resolved (any member can resolve)."""
    await require_role(session, org_id, actor_id, Role.member)
    await _get(session, org_id=org_id, annotation_id=annotation_id)
    by = resolved_by_identity_id or await _user_identity_id(
        session, org_id=org_id, actor_id=actor_id
    )
    new_version = await optimistic_update(
        session,
        Annotation,
        pk=annotation_id,
        expected_version=expected_version,
        values={
            "status": "resolved",
            "resolved_at": dt.datetime.now(dt.UTC),
            "resolved_by_identity_id": by,
        },
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="annotation",
        entity_id=annotation_id,
        action="resolve",
    )
    await _log_annotation_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        annotation_id=annotation_id,
        version_from=expected_version,
        version_to=new_version,
        changed_fields=["status"],
    )
    return new_version


async def reopen(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    annotation_id: uuid.UUID,
    expected_version: int,
) -> int:
    await require_role(session, org_id, actor_id, Role.member)
    await _get(session, org_id=org_id, annotation_id=annotation_id)
    new_version = await optimistic_update(
        session,
        Annotation,
        pk=annotation_id,
        expected_version=expected_version,
        values={"status": "open", "resolved_at": None, "resolved_by_identity_id": None},
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="annotation",
        entity_id=annotation_id,
        action="reopen",
    )
    await _log_annotation_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        annotation_id=annotation_id,
        version_from=expected_version,
        version_to=new_version,
        changed_fields=["status"],
    )
    return new_version


# --------------------------------------------------------------------------
# suggestion accept / reject
# --------------------------------------------------------------------------
async def _apply_to_document(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    ann: Annotation,
) -> None:
    """Splice the accepted suggestion into the live document body, via
    the owning service (optimistic-concurrency aware). Raises
    SUGGESTION_STALE when the target text can no longer be located."""
    original = ann.original_text or ""
    proposed = ann.proposed_text or ""
    if ann.doc_kind == "note_part":
        # Lazy import: avoid a core import cycle (note_parts -> notes).
        from mycelium_core.services import note_parts as _np

        part_id = cast(uuid.UUID, ann.note_part_id)  # XOR CHECK: set for note_part
        part = await _np.get_part(session, org_id=org_id, part_id=part_id)
        new_body = _splice(
            part.body,
            original=original,
            proposed=proposed,
            prefix=ann.anchor_prefix,
            suffix=ann.anchor_suffix,
            domain=ann.anchor_domain,
        )
        if new_body is None:
            raise DomainError(MessageCode.SUGGESTION_STALE)
        await _np.update_part(
            session,
            org_id=org_id,
            actor_id=actor_id,
            part_id=part_id,
            expected_version=part.version,
            body=new_body,
        )
        return
    # task_description
    task_id = cast(uuid.UUID, ann.task_id)  # XOR CHECK: set for task_description
    task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if task is None:
        raise NotFoundError(MessageCode.TASK_NOT_FOUND)
    new_desc = _splice(
        task.description or "",
        original=original,
        proposed=proposed,
        prefix=ann.anchor_prefix,
        suffix=ann.anchor_suffix,
        domain=ann.anchor_domain,
    )
    if new_desc is None:
        raise DomainError(MessageCode.SUGGESTION_STALE)
    await optimistic_update(
        session,
        Task,
        pk=task_id,
        expected_version=task.version,
        values={"description": new_desc},
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="update",
        diff={"via": "suggestion_accept", "fields": "description"},
    )


def _body_write_scope(doc_kind: str) -> str:
    """The scope that WRITING the target document body requires: a note part
    body is ``notes:write``, a task description ``tasks:write``. Used by the
    accept fence below."""
    return "notes:write" if doc_kind == "note_part" else "tasks:write"


async def accept_suggestion(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    annotation_id: uuid.UUID,
    expected_version: int,
    resolved_by_identity_id: uuid.UUID | None = None,
    granted_scopes: list[str] | None = None,
) -> int:
    """Apply the suggestion to the document body, then mark it accepted.
    Raises SUGGESTION_STALE (and changes nothing) if the target text has
    moved or gone.

    ``granted_scopes`` is the caller's scope list when it is a scoped
    assistant (``None`` for a human session or a bare token = full access).
    Accepting splices the proposed text INTO the note part / task description,
    so a scoped caller must ALSO hold write on that family: without this fence,
    ``comments:write`` alone (enough to accept) would mutate document content,
    making propose-then-accept a bypass of ``notes:write`` / ``tasks:write``
    (task c19f2f63, enabler B). Enforced here, next to the role check, so it
    holds identically over every transport (MCP tool and REST route)."""
    await require_role(session, org_id, actor_id, Role.member)
    ann = await _get(session, org_id=org_id, annotation_id=annotation_id)
    if ann.kind != "suggestion":
        raise DomainError(MessageCode.ANNOTATION_NOT_SUGGESTION)
    if ann.status != "open":
        raise DomainError(MessageCode.SUGGESTION_NOT_PENDING)
    if granted_scopes is not None:
        need = _body_write_scope(ann.doc_kind)
        if need not in granted_scopes:
            raise ForbiddenError(MessageCode.AGENT_SCOPE_DENIED, scope=need)
    await _apply_to_document(session, org_id=org_id, actor_id=actor_id, ann=ann)
    by = resolved_by_identity_id or await _user_identity_id(
        session, org_id=org_id, actor_id=actor_id
    )
    new_version = await optimistic_update(
        session,
        Annotation,
        pk=annotation_id,
        expected_version=expected_version,
        values={
            "status": "accepted",
            "resolved_at": dt.datetime.now(dt.UTC),
            "resolved_by_identity_id": by,
        },
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="annotation",
        entity_id=annotation_id,
        action="accept",
    )
    await _log_annotation_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        annotation_id=annotation_id,
        version_from=expected_version,
        version_to=new_version,
        changed_fields=["status"],
    )
    return new_version


async def reject_suggestion(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    annotation_id: uuid.UUID,
    expected_version: int,
    resolved_by_identity_id: uuid.UUID | None = None,
) -> int:
    """Reject a pending suggestion; the document body is untouched."""
    await require_role(session, org_id, actor_id, Role.member)
    ann = await _get(session, org_id=org_id, annotation_id=annotation_id)
    if ann.kind != "suggestion":
        raise DomainError(MessageCode.ANNOTATION_NOT_SUGGESTION)
    if ann.status != "open":
        raise DomainError(MessageCode.SUGGESTION_NOT_PENDING)
    by = resolved_by_identity_id or await _user_identity_id(
        session, org_id=org_id, actor_id=actor_id
    )
    new_version = await optimistic_update(
        session,
        Annotation,
        pk=annotation_id,
        expected_version=expected_version,
        values={
            "status": "rejected",
            "resolved_at": dt.datetime.now(dt.UTC),
            "resolved_by_identity_id": by,
        },
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="annotation",
        entity_id=annotation_id,
        action="reject",
    )
    await _log_annotation_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        annotation_id=annotation_id,
        version_from=expected_version,
        version_to=new_version,
        changed_fields=["status"],
    )
    return new_version


# --------------------------------------------------------------------------
# per-user UI state (card collapse/expand; mirrors services/note_parts)
# --------------------------------------------------------------------------
async def set_ui_state(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    annotation_id: uuid.UUID,
    collapsed: bool,
) -> AnnotationUIState:
    """Upsert the caller's collapse state for one annotation card.

    User-scoped, last-write-wins (no version): this is presentation state,
    not content, so it never conflicts with a concurrent body edit. No row
    means expanded; the row is materialised lazily on the first toggle.
    """
    await require_role(session, org_id, user_id, Role.member)
    # Defensive presence check so a foreign/unknown id is a clean 404
    # (RLS would otherwise let the bare upsert fail on the FK).
    await _get(session, org_id=org_id, annotation_id=annotation_id)
    stmt = (
        pg_insert(AnnotationUIState)
        .values(user_id=user_id, annotation_id=annotation_id, collapsed=collapsed)
        .on_conflict_do_update(
            index_elements=[AnnotationUIState.user_id, AnnotationUIState.annotation_id],
            set_={"collapsed": collapsed, "updated_at": func.now()},
        )
        .returning(AnnotationUIState)
    )
    return (await session.execute(stmt)).scalar_one()


async def set_ui_states_bulk(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    doc_kind: str,
    doc_id: uuid.UUID,
    collapsed: bool,
) -> dict[uuid.UUID, bool]:
    """Collapse/expand every top-level (non-deleted) card on a document for
    the caller in a single multi-row upsert (the panel's collapse-all).

    Replies are deliberately left out: folding a thread is the root card's
    job, and bulldozing every reply's own state would mean re-expanding a
    thread one reply at a time afterwards. A reply folded by hand keeps
    that state across collapse-all / expand-all."""
    await require_role(session, org_id, user_id, Role.member)
    await _resolve_doc(session, org_id=org_id, doc_kind=doc_kind, doc_id=doc_id)
    rows = await list_for_doc(session, org_id=org_id, doc_kind=doc_kind, doc_id=doc_id)
    ids = [a.id for a in rows if a.parent_id is None]
    if not ids:
        return {}
    stmt = (
        pg_insert(AnnotationUIState)
        .values([{"user_id": user_id, "annotation_id": aid, "collapsed": collapsed} for aid in ids])
        .on_conflict_do_update(
            index_elements=[AnnotationUIState.user_id, AnnotationUIState.annotation_id],
            set_={"collapsed": collapsed, "updated_at": func.now()},
        )
    )
    await session.execute(stmt)
    return {aid: collapsed for aid in ids}


async def get_ui_states_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    doc_kind: str,
    doc_id: uuid.UUID,
) -> dict[uuid.UUID, bool]:
    """``{annotation_id: collapsed}`` for the caller on one document. No
    ``org_id`` parameter: RLS on both tables scopes the join, ``user_id`` is
    the explicit filter; an id absent from the map means expanded."""
    if doc_kind not in ANNOTATION_DOC_KINDS:
        raise DomainError(MessageCode.ANNOTATION_DOC_KIND_INVALID)
    stmt = (
        select(AnnotationUIState.annotation_id, AnnotationUIState.collapsed)
        .join(Annotation, Annotation.id == AnnotationUIState.annotation_id)
        .where(AnnotationUIState.user_id == user_id, Annotation.doc_kind == doc_kind)
    )
    if doc_kind == "task_description":
        stmt = stmt.where(Annotation.task_id == doc_id)
    else:
        stmt = stmt.where(Annotation.note_part_id == doc_id)
    rows = (await session.execute(stmt)).all()
    return {aid: bool(collapsed) for aid, collapsed in rows}


async def get_ui_states_by_ids(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    annotation_ids: list[uuid.UUID],
) -> dict[uuid.UUID, bool]:
    """``{annotation_id: collapsed}`` for the caller over an explicit id set
    (the single-card GET and the cross-document assigned inbox, where the
    per-doc helper doesn't apply). Ids without a row are simply absent."""
    if not annotation_ids:
        return {}
    rows = (
        await session.execute(
            select(AnnotationUIState.annotation_id, AnnotationUIState.collapsed).where(
                AnnotationUIState.user_id == user_id,
                AnnotationUIState.annotation_id.in_(annotation_ids),
            )
        )
    ).all()
    return {aid: bool(collapsed) for aid, collapsed in rows}
