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
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.concurrency import optimistic_update
from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.annotation import ANNOTATION_DOC_KINDS, Annotation
from flow_core.models.identity import Identity, IdentityKind
from flow_core.models.membership import Role
from flow_core.models.note_part import NotePart
from flow_core.models.task import Task
from flow_core.services import audit, md_anchor
from flow_core.services import identities as identities_svc
from flow_core.services.rbac import require_role


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
    # note_part
    found = (
        await session.execute(select(NotePart.id).where(NotePart.id == doc_id))
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
) -> Annotation:
    ann = (
        await session.execute(select(Annotation).where(Annotation.id == annotation_id))
    ).scalar_one_or_none()
    if ann is None:
        raise NotFoundError(MessageCode.ANNOTATION_NOT_FOUND)
    if ann.deleted_at is not None and not include_deleted:
        raise NotFoundError(MessageCode.ANNOTATION_NOT_FOUND)
    return ann


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
) -> str | None:
    """Apply ``original -> proposed`` to the markdown ``body`` at the
    anchor pinned by the (rendered-domain) original + optional
    prefix/suffix. Returns the new body, or None when the anchor can no
    longer be located faithfully (the suggestion is stale).

    Delegates to ``md_anchor``: the quote/prefix/suffix the SPA captured
    are *rendered* text (markdown stripped, links->text, blocks joined by
    a space), so they are resolved in that same rendered domain and mapped
    back to the markdown source. This is what makes accept faithful across
    inline formatting (``**bold**``, links, code, math) and multi-block
    selections; a non-locatable or structure-changing splice declines to
    None rather than corrupt the body. No persisted offsets: the map is
    recomputed from the live body, so the anchor survives prior edits."""
    return md_anchor.splice(
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
        original_text=original_text,
        # Normalise edge whitespace: a proposed replacement for an inline
        # rendered span carries no meaning in leading/trailing spaces (the
        # markdown renderer strips them), yet an un-stripped value makes the
        # accept-time re-render gate asymmetric and STALEs an otherwise
        # valid suggestion. One source point covers web/CLI/MCP authors.
        proposed_text=proposed_text.strip(),
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
) -> list[Annotation]:
    """Every annotation on a markdown document, oldest first (so a task's
    general comments read as a chronological work diary)."""
    if doc_kind not in ANNOTATION_DOC_KINDS:
        raise DomainError(MessageCode.ANNOTATION_DOC_KIND_INVALID)
    stmt = select(Annotation).where(Annotation.doc_kind == doc_kind)
    if doc_kind == "task_description":
        stmt = stmt.where(Annotation.task_id == doc_id)
    else:
        stmt = stmt.where(Annotation.note_part_id == doc_id)
    if not include_deleted:
        stmt = stmt.where(Annotation.deleted_at.is_(None))
    if not include_resolved:
        stmt = stmt.where(Annotation.status == "open")
    stmt = stmt.order_by(Annotation.created_at)
    return list((await session.execute(stmt)).scalars().all())


async def get_annotation(
    session: AsyncSession, *, org_id: uuid.UUID, annotation_id: uuid.UUID
) -> Annotation:
    return await _get(session, org_id=org_id, annotation_id=annotation_id)


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
    stmt = select(Annotation).where(
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
    return new_version


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
    return new_version


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
        from flow_core.services import note_parts as _np

        part_id = cast(uuid.UUID, ann.note_part_id)  # XOR CHECK: set for note_part
        part = await _np.get_part(session, org_id=org_id, part_id=part_id)
        new_body = _splice(
            part.body,
            original=original,
            proposed=proposed,
            prefix=ann.anchor_prefix,
            suffix=ann.anchor_suffix,
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


async def accept_suggestion(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    annotation_id: uuid.UUID,
    expected_version: int,
    resolved_by_identity_id: uuid.UUID | None = None,
) -> int:
    """Apply the suggestion to the document body, then mark it accepted.
    Raises SUGGESTION_STALE (and changes nothing) if the target text has
    moved or gone."""
    await require_role(session, org_id, actor_id, Role.member)
    ann = await _get(session, org_id=org_id, annotation_id=annotation_id)
    if ann.kind != "suggestion":
        raise DomainError(MessageCode.ANNOTATION_NOT_SUGGESTION)
    if ann.status != "open":
        raise DomainError(MessageCode.SUGGESTION_NOT_PENDING)
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
    return new_version
