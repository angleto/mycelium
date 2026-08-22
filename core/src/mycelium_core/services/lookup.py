"""Prefix resolver for entity IDs.

Roadmap notes refer to tasks/notes by an 8-char UUID prefix in backticks
(e.g. ``91cf6aaa``). The backend already finds them via free-text
search; this service exposes the same capability as a deterministic,
prefix-only lookup so the SPA can:

* turn ``code`` markdown nodes into clickable chips (one round trip per
  prefix, batched by the renderer);
* redirect short URLs ``/n/<prefix>`` / ``/t/<prefix>`` to canonical
  routes;
* upgrade in-place when the user types a prefix into ``/notes/<id>`` /
  ``/tasks/<id>``.

RLS is enforced implicitly by the tenant session (ADR-0015): every
query inherits the workspace scope, so the resolver never reaches
across orgs.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.note import Note
from mycelium_core.models.task import Task
from mycelium_core.models.workflow import WorkflowState
from mycelium_core.services.note_effective import effective_note_clause

# Accept either a raw hex run (``91cf6aaa``) or a partial UUID with
# dashes (``91cf6aaa-9abc``). Min 4 hex chars is the smallest prefix
# that has a useful chance of being unique; 8 is the convention. Max 36
# matches a full canonical UUID. We intentionally don't strip dashes:
# ``id::text`` returns the canonical form, and the SQL LIKE is char-
# accurate, so we want the caller to use the canonical form when going
# past the first dash boundary.
_PREFIX_RE = re.compile(r"^[0-9a-f][0-9a-f-]{2,34}[0-9a-f]$", re.IGNORECASE)
MIN_PREFIX_LEN = 4
MAX_PREFIX_LEN = 36

# A whole query that IS an entity code, for the search surfaces. Searching
# an id is a LOOKUP, not a similarity question: the only meaningful match
# is exact, so the caller answers it by resolving the prefix (and by exact
# literal occurrences of the string) instead of embedding it. An 8-char
# hex token has no semantic content, but an embedder still returns its
# nearest neighbours, which are arbitrary -- that is how `5d44d8e5` came
# back with five confident, unrelated results.
#
# The floor is 8 hex digits, the ADR-0038 convention, and NOT the 4 that
# ``normalise_prefix`` accepts: shorter runs collide with ordinary words
# spelled in hex ('cafe', 'decade', 'faced', 'added'), which must keep
# going through the normal pipeline. At 8+ a collision ('deadbeef') is
# rare, and answering it with exact matches is still sensible.
IDENTIFIER_MIN_HEX = 8


def looks_like_entity_code(raw: str) -> bool:
    """True when the whole query is an entity-code lookup (see above).

    Deliberately strict: one token, no whitespace, hex with optional
    dashes, at least ``IDENTIFIER_MIN_HEX`` hex digits, no longer than a
    canonical UUID."""
    s = raw.strip().lower()
    if not s or len(s) > MAX_PREFIX_LEN or any(ch.isspace() for ch in s):
        return False
    if len(s.replace("-", "")) < IDENTIFIER_MIN_HEX:
        return False
    return bool(_PREFIX_RE.match(s))


@dataclass(frozen=True, slots=True)
class LookupMatch:
    kind: str  # "task" | "note"
    id: uuid.UUID
    title: str | None
    state_name: str | None
    is_terminal: bool | None
    is_archived: bool
    is_deleted: bool


def normalise_prefix(raw: str) -> str:
    """Reject prefixes that can't possibly be a UUID fragment. Returns
    the canonical form (lowercase, no surrounding whitespace).

    Raised errors carry ``MessageCode.DOMAIN_ERROR`` so the API
    handler maps them to 400.
    """
    p = raw.strip().lower()
    if len(p) < MIN_PREFIX_LEN or len(p) > MAX_PREFIX_LEN:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    if not _PREFIX_RE.match(p):
        raise DomainError(MessageCode.DOMAIN_ERROR)
    return p


async def _state_map(
    session: AsyncSession, state_ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, bool]]:
    if not state_ids:
        return {}
    rows = (
        await session.execute(
            select(WorkflowState.id, WorkflowState.name, WorkflowState.is_terminal).where(
                WorkflowState.id.in_(state_ids)
            )
        )
    ).all()
    return {sid: (name, bool(term)) for sid, name, term in rows}


async def resolve_prefix(
    session: AsyncSession,
    *,
    prefix: str,
    kinds: tuple[str, ...] = ("task", "note"),
    include_archived: bool = False,
    include_deleted: bool = False,
    limit: int = 20,
) -> list[LookupMatch]:
    """Return at most ``limit`` matches whose UUID starts with ``prefix``.

    ``prefix`` is taken verbatim from ``normalise_prefix`` and used as a
    ``LIKE 'prefix%'`` filter on ``id::text``. Order is ``kind`` first
    (tasks before notes, mirroring the most common authoring intent in
    roadmap notes), then most-recent ``updated_at`` so the freshest
    candidate wins the disambiguator's first row.

    ``include_archived`` / ``include_deleted`` mean the SAME thing for
    both kinds: excluded by default, opted into by the flag, and always
    reported on the match. Callers whose question is "what entity is this
    id?" -- chip rendering, the short URLs, the id normaliser -- pass
    ``include_archived=True``, because a shelved entity is still that
    entity; callers offering a LIST of candidates (the mention picker,
    the search surfaces) keep the default, matching what ``list_notes``
    and ``task_search`` show.

    The query relies on the existing UUID PK index: PostgreSQL can use
    a B-tree index on a text-cast UUID column only via a functional
    index, which we don't have. The expected payload size (8-char
    prefix, scoped to one workspace by RLS) keeps the sequential scan
    cheap; if profiles ever show this hot, the migration is a single
    expression index ``ON tasks ((id::text) text_pattern_ops)``.
    """
    p = normalise_prefix(prefix)
    pat = p + "%"

    out: list[LookupMatch] = []

    if "task" in kinds:
        q = select(Task.id, Task.title, Task.state_id, Task.is_archived, Task.deleted_at).where(
            text("tasks.id::text LIKE :pat").bindparams(pat=pat)
        )
        if not include_archived:
            q = q.where(Task.is_archived.is_(False))
        if not include_deleted:
            q = q.where(Task.deleted_at.is_(None))
        q = q.order_by(Task.updated_at.desc()).limit(limit)
        rows = (await session.execute(q)).all()
        state_ids = {sid for _, _, sid, _, _ in rows if sid is not None}
        smap = await _state_map(session, state_ids)
        for tid, title, sid, archived, deleted_at in rows:
            sname, term = smap.get(sid, (None, None))
            out.append(
                LookupMatch(
                    kind="task",
                    id=tid,
                    title=title,
                    state_name=sname,
                    is_terminal=term,
                    is_archived=bool(archived),
                    is_deleted=deleted_at is not None,
                )
            )

    if "note" in kinds and len(out) < limit:
        # Distinct local name from the task branch: the two selects have
        # different row shapes and reusing ``q`` makes mypy infer the
        # broader (and wrong) union, breaking strict mode.
        qn = select(Note.id, Note.title, Note.is_archived, Note.deleted_at).where(
            text("notes.id::text LIKE :pat").bindparams(pat=pat)
        )
        # The effective-note predicate: a trashed note only with the opt-in,
        # a 'proposed' one (autonomously generated, pending review) never --
        # the @-mention picker must not offer a node nobody can open.
        qn = qn.where(effective_note_clause(include_deleted=include_deleted))
        # ``is_archived`` is NOT part of that predicate (see
        # ``note_effective``: archiving is a shelf, not a bin, and an
        # archived note stays a full graph node). It is the same
        # PRESENTATION axis the task branch above applies, and it applies
        # here on the same terms: hidden by default, opted into by the
        # flag, and REPORTED either way. It used to be neither -- the
        # column was not even selected, so ``include_archived`` was
        # silently ignored for notes and every match claimed
        # ``is_archived=False``, which is how an archived note reached the
        # picker labelled as live (task d12f6217).
        if not include_archived:
            qn = qn.where(Note.is_archived.is_(False))
        qn = qn.order_by(Note.updated_at.desc()).limit(limit - len(out))
        note_rows = (await session.execute(qn)).all()
        for nid, title, archived, deleted_at in note_rows:
            out.append(
                LookupMatch(
                    kind="note",
                    id=nid,
                    title=title,
                    state_name=None,
                    is_terminal=None,
                    is_archived=bool(archived),
                    is_deleted=deleted_at is not None,
                )
            )

    return out[:limit]
