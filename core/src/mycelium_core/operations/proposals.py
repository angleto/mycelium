"""Garden proposals as one kind of object, and a note's history (ADR-0075).

One listing and the same three verbs for every mechanism that proposes
changes to the notes (generated notes, link proposals, facts), and the
history of what happened to a note. Bound to REST and MCP by the operation
registry (ADR-0068).
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from pydantic import BaseModel, Field

from mycelium_core.mcp_scopes import HUMAN_ONLY
from mycelium_core.operations import Kind, OpCtx, operation, paging
from mycelium_core.services import decomposition as decomposition_svc
from mycelium_core.services import note_history as history_svc
from mycelium_core.services import proposals as proposals_svc
from mycelium_core.services.proposals import Listing, ProposalKind, Verdict

GARDEN = "garden"


class ProposalNoteRef(BaseModel):
    note_id: uuid.UUID
    title: str | None = None


class ProposalOut(BaseModel):
    """One garden proposal. ``kind``: ``synthesis`` (a generated note: read
    it whole with garden_review_read), ``link`` (a link between the two
    ``subjects``; accepting needs one of ``link_kinds``), ``fact`` (a fact
    read from the note in ``subjects``). ``state``: ``pending``,
    ``declined`` or ``accepted``. ``reopenable``: whether a decision can be
    taken back."""

    kind: ProposalKind
    id: uuid.UUID
    version: int | None = None
    state: str
    created_at: datetime.datetime
    decided_at: datetime.datetime | None = None
    subjects: list[ProposalNoteRef]
    proposer: str | None = None
    reopenable: bool
    title: str | None = None
    preview: str | None = None
    humus_kind: str | None = None
    fact_subject: str | None = None
    fact_predicate: str | None = None
    fact_object: str | None = None
    reason: str | None = None
    link_kinds: list[str] = []


class ProposalListIn(BaseModel):
    listing: Listing = Listing.pending
    kinds: list[ProposalKind] | None = None
    limit: int = Field(50, ge=1, le=200)
    cursor: str | None = None


class ProposalPage(BaseModel):
    items: list[ProposalOut]
    next_cursor: str | None = None


class ProposalDecideIn(BaseModel):
    """``expected_version`` is required for a synthesis (the text read) and
    a fact; ``link_kind`` to accept a link."""

    kind: ProposalKind
    id: uuid.UUID
    verdict: Verdict
    expected_version: int | None = None
    link_kind: str | None = Field(None, max_length=20)


class ProposalRefIn(BaseModel):
    kind: ProposalKind
    id: uuid.UUID


class ProposalActionOut(BaseModel):
    kind: ProposalKind
    id: uuid.UUID
    done: bool


def _after(op_id: str, cursor: str | None) -> tuple[datetime.datetime, uuid.UUID] | None:
    if not cursor:
        return None
    try:
        at_raw, id_raw = paging.decode_cursor(cursor)
        return datetime.datetime.fromisoformat(at_raw), uuid.UUID(id_raw)
    except (ValueError, TypeError) as exc:
        raise paging.invalid(op_id, "cursor: not one this list issued") from exc


def _out(p: Any) -> ProposalOut:
    return ProposalOut(
        kind=p.kind,
        id=p.id,
        version=p.version,
        state=p.state,
        created_at=p.created_at,
        decided_at=p.decided_at,
        subjects=[ProposalNoteRef(note_id=s.note_id, title=s.title) for s in p.subjects],
        proposer=p.proposer,
        reopenable=p.reopenable,
        title=p.title,
        preview=p.preview,
        humus_kind=p.humus_kind,
        fact_subject=p.fact_subject,
        fact_predicate=p.fact_predicate,
        fact_object=p.fact_object,
        reason=p.reason,
        link_kinds=list(p.link_kinds),
    )


@operation(
    "garden.proposals.list",
    domain=GARDEN,
    kind=Kind.READ,
    access="notes:read",
    params=ProposalListIn,
    result=ProposalPage,
)
async def proposals_list(ctx: OpCtx, p: ProposalListIn) -> ProposalPage:
    """Everything the garden proposes, of every kind, in one list: what
    waits for a decision (``listing='pending'``, newest first) or what was
    decided (``'decided'``, newest decision first). Narrow with ``kinds``.
    Pass ``next_cursor`` back as ``cursor`` for the next page."""
    items, nxt = await proposals_svc.list_proposals(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.actor_id,
        listing=p.listing,
        kinds=set(p.kinds) if p.kinds else None,
        limit=p.limit,
        after=_after("garden.proposals.list", p.cursor),
    )
    return ProposalPage(
        items=[_out(x) for x in items],
        next_cursor=paging.encode_cursor([nxt[0], nxt[1]]) if nxt else None,
    )


@operation(
    "garden.proposals.decide",
    domain=GARDEN,
    kind=Kind.WRITE,
    access=HUMAN_ONLY,
    params=ProposalDecideIn,
    result=ProposalActionOut,
)
async def proposals_decide(ctx: OpCtx, p: ProposalDecideIn) -> ProposalActionOut:
    """Accept or decline one proposal, of any kind. A person's act: an
    assistant decides only what the person handed over
    (garden_delegated_decide)."""
    await proposals_svc.decide(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.actor_id,
        kind=p.kind,
        proposal_id=p.id,
        verdict=p.verdict,
        expected_version=p.expected_version,
        link_kind=p.link_kind,
    )
    return ProposalActionOut(kind=p.kind, id=p.id, done=True)


@operation(
    "garden.proposals.reopen",
    domain=GARDEN,
    kind=Kind.WRITE,
    access=HUMAN_ONLY,
    params=ProposalRefIn,
    result=ProposalActionOut,
)
async def proposals_reopen(ctx: OpCtx, p: ProposalRefIn) -> ProposalActionOut:
    """Take a decision back: the proposal is pending again, as before (an
    accepted link is removed, an approved note or fact leaves the memory, a
    declined one comes back). A declined fact cannot be reopened."""
    await proposals_svc.reopen(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.actor_id, kind=p.kind, proposal_id=p.id
    )
    return ProposalActionOut(kind=p.kind, id=p.id, done=True)


class NoteHistoryIn(BaseModel):
    note_id: uuid.UUID
    limit: int = Field(50, ge=1, le=200)
    cursor: str | None = None


class NoteHistoryEventOut(BaseModel):
    """One thing that happened to the note. ``action`` is the code of the
    activity log (``set_maturity``, ``auto_maturity``, ``auto_dormant``,
    ``garden_review:source_humus``, ``archive``, ...); ``actor_kind`` says
    whether a person, an agent or the system did it; ``changes`` holds the
    before and after of the fields the garden moves."""

    event_id: uuid.UUID
    at: datetime.datetime
    action: str
    actor_kind: str
    changes: dict[str, dict[str, Any]]
    about_note_id: uuid.UUID | None = None


class NoteHistoryPage(BaseModel):
    items: list[NoteHistoryEventOut]
    next_cursor: str | None = None


@operation(
    "garden.note_history",
    domain=GARDEN,
    kind=Kind.READ,
    access="notes:read",
    params=NoteHistoryIn,
    result=NoteHistoryPage,
)
async def note_history(ctx: OpCtx, p: NoteHistoryIn) -> NoteHistoryPage:
    """What happened to a note, newest first: maturity moves by a person or
    by the garden, the humus mark, archiving, review decisions about what was
    generated from it. Pass ``next_cursor`` back as ``cursor``."""
    items, nxt = await history_svc.note_history(
        ctx.session,
        org_id=ctx.org_id,
        note_id=p.note_id,
        limit=p.limit,
        after=_after("garden.note_history", p.cursor),
    )
    return NoteHistoryPage(
        items=[
            NoteHistoryEventOut(
                event_id=e.event_id,
                at=e.at,
                action=e.action,
                actor_kind=e.actor_kind,
                changes=e.changes,
                about_note_id=e.about_note_id,
            )
            for e in items
        ],
        next_cursor=paging.encode_cursor([nxt[0], nxt[1]]) if nxt else None,
    )


# --------------------------------------------------------------------------
# Patterns and seasons on demand (ADR-0075 decision 6)
# --------------------------------------------------------------------------
#
# The garden suggested patterns and seasons and the web had no way to act
# on them: the only doors were two hand-written MCP tools. They are registry
# operations now, so the same operation serves the web and MCP, and the
# MCP tool names are unchanged.


class PatternIn(BaseModel):
    """``source_note_ids``: two or more archived notes. ``synthesized_text``
    and ``origin_model_id``: write the pattern with your own model instead
    of the workspace's (ADR-0076); its grounding is then yours to
    guarantee."""

    source_note_ids: list[uuid.UUID] = Field(min_length=2, max_length=50)
    synthesized_text: str | None = Field(None, max_length=20000)
    origin_model_id: str | None = Field(None, max_length=96)


class PatternOut(BaseModel):
    pattern_note_id: uuid.UUID
    model_id: str
    created: bool


class SeasonIn(BaseModel):
    year: int = Field(ge=1970, le=9999)
    quarter: int = Field(ge=1, le=4)
    synthesized_text: str | None = Field(None, max_length=20000)
    origin_model_id: str | None = Field(None, max_length=96)


class SeasonOut(BaseModel):
    season_note_id: uuid.UUID
    model_id: str
    created: bool


@operation(
    "garden.synthesize.pattern",
    domain=GARDEN,
    kind=Kind.WRITE,
    access="ai:generate",
    params=PatternIn,
    result=PatternOut,
)
async def extract_cluster_pattern(ctx: OpCtx, p: PatternIn) -> PatternOut:
    """Synthesise a ``pattern`` over a set of ARCHIVED notes: the
    through-lines across them, as a new note linked back to each source. Born
    pending: the person approves it (garden_review_pending), and the sources
    are never changed. Idempotent on the source set (``created`` false on a
    re-run)."""
    res = await decomposition_svc.extract_cluster_pattern(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.actor_id,
        source_note_ids=list(p.source_note_ids),
        synthesized_text=p.synthesized_text,
        origin_model_id=p.origin_model_id,
    )
    return PatternOut(pattern_note_id=res.note_id, model_id=res.model_id, created=res.created)


@operation(
    "garden.synthesize.season",
    domain=GARDEN,
    kind=Kind.WRITE,
    access="ai:generate",
    params=SeasonIn,
    result=SeasonOut,
)
async def synthesize_season(ctx: OpCtx, p: SeasonIn) -> SeasonOut:
    """Synthesise a ``season`` for one quarter, "what I cultivated this
    season", over the notes archived in it. Born pending, never a change to
    the sources. Idempotent per (year, quarter)."""
    res = await decomposition_svc.synthesize_season(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.actor_id,
        year=p.year,
        quarter=p.quarter,
        synthesized_text=p.synthesized_text,
        origin_model_id=p.origin_model_id,
    )
    return SeasonOut(season_note_id=res.note_id, model_id=res.model_id, created=res.created)
