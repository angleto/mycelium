"""One kind of object for everything the garden proposes (ADR-0075, decision 1).

Several mechanisms propose changes to a person's notes, each with its own
storage and invariants: generated notes (ADR-0043), link proposals
(ADR-0073), facts of the knowledge graph (ADR-0071). The person saw each
through its own surface, verbs and reversibility. This package gives them one
contract, the same verbs and one place:

- a ``Proposal`` row says what kind of thing is proposed, about which notes,
  by whom, in which state, with what the person needs to decide it;
- the verbs are ``accept``, ``decline`` and ``reopen`` (take a decision
  back), the same for every kind; a kind refuses what it cannot do;
- each mechanism keeps its storage and its rules and publishes an
  ``ADAPTER`` in a module of this package. Adapters are found by presence:
  adding a mechanism is adding a module, and a broken one fails alone.

The listing is keyset-paged on ``(created_at, id)`` across every kind, newest
first, so a page boundary is stable while proposals arrive.
"""

from __future__ import annotations

import datetime as dt
import enum
import importlib
import logging
import pkgutil
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode

log = logging.getLogger(__name__)


class ProposalKind(enum.StrEnum):
    synthesis = "synthesis"
    link = "link"
    fact = "fact"


class Listing(enum.StrEnum):
    #: Waiting for a decision.
    pending = "pending"
    #: Decided by a person or an agent, newest decision first.
    decided = "decided"


class Verdict(enum.StrEnum):
    accept = "accept"
    decline = "decline"


@dataclass(frozen=True)
class NoteRef:
    note_id: uuid.UUID
    title: str | None


@dataclass(frozen=True)
class Proposal:
    """One proposal, whatever proposed it. ``state`` is ``pending``,
    ``declined``, ``accepted`` (a synthesis approved, a link accepted, a fact
    approved) ; ``reopenable`` says whether ``reopen`` can take the decision
    back (a declined fact cannot: it is frozen)."""

    kind: ProposalKind
    id: uuid.UUID
    version: int | None
    state: str
    created_at: dt.datetime
    decided_at: dt.datetime | None
    #: The notes the proposal is about: the sources of a synthesis, the two
    #: ends of a link, the note a fact was read from.
    subjects: list[NoteRef]
    #: Who proposed it: a model id or a proposer's name.
    proposer: str | None
    reopenable: bool
    title: str | None = None
    preview: str | None = None
    humus_kind: str | None = None
    #: A fact's three parts, named.
    fact_subject: str | None = None
    fact_predicate: str | None = None
    fact_object: str | None = None
    #: The proposer's stated reason, when it gives one.
    reason: str | None = None
    #: The kinds of link the person may choose when accepting a link.
    link_kinds: list[str] = field(default_factory=list)


Cursor = tuple[dt.datetime, uuid.UUID]


class Adapter(Protocol):
    kind: ProposalKind

    async def list(
        self,
        session: AsyncSession,
        *,
        org_id: uuid.UUID,
        actor_id: uuid.UUID,
        listing: Listing,
        limit: int,
        after: Cursor | None,
    ) -> list[Proposal]: ...

    async def decide(
        self,
        session: AsyncSession,
        *,
        org_id: uuid.UUID,
        actor_id: uuid.UUID,
        proposal_id: uuid.UUID,
        verdict: Verdict,
        expected_version: int | None,
        link_kind: str | None,
    ) -> None: ...

    async def reopen(
        self,
        session: AsyncSession,
        *,
        org_id: uuid.UUID,
        actor_id: uuid.UUID,
        proposal_id: uuid.UUID,
    ) -> None: ...


def _discover() -> dict[ProposalKind, Adapter]:
    found: dict[ProposalKind, Adapter] = {}
    for mod in pkgutil.iter_modules(__path__):
        try:
            module = importlib.import_module(f"{__name__}.{mod.name}")
        except Exception:  # a broken adapter fails alone and is reported
            log.exception("proposal adapter %s failed to load", mod.name)
            continue
        adapter = getattr(module, "ADAPTER", None)
        if adapter is not None:
            found[adapter.kind] = adapter
    return found


_ADAPTERS: dict[ProposalKind, Adapter] | None = None


def adapters() -> dict[ProposalKind, Adapter]:
    global _ADAPTERS
    if _ADAPTERS is None:
        _ADAPTERS = _discover()
    return _ADAPTERS


def _adapter(kind: ProposalKind) -> Adapter:
    a = adapters().get(kind)
    if a is None:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    return a


async def list_proposals(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    listing: Listing,
    kinds: set[ProposalKind] | None = None,
    limit: int = 50,
    after: Cursor | None = None,
) -> tuple[list[Proposal], Cursor | None]:
    """A page of proposals of every kind (or of ``kinds``), newest first by
    creation for the pending ones and by decision for the decided ones, and
    the cursor of the next page (None at the end). Each adapter is asked for
    ``limit + 1`` rows past the cursor, so the merge knows whether more
    exist without counting."""
    rows: list[Proposal] = []
    for kind, adapter in sorted(adapters().items()):
        if kinds and kind not in kinds:
            continue
        rows.extend(
            await adapter.list(
                session,
                org_id=org_id,
                actor_id=actor_id,
                listing=listing,
                limit=limit + 1,
                after=after,
            )
        )
    rows.sort(key=lambda p: (sort_key(p, listing), str(p.id)), reverse=True)
    page = rows[:limit]
    nxt = (sort_key(page[-1], listing), page[-1].id) if len(rows) > limit else None
    return page, nxt


def sort_key(p: Proposal, listing: Listing) -> dt.datetime:
    """The instant a listing orders by: creation while pending, the decision
    once decided."""
    if listing is Listing.decided and p.decided_at is not None:
        return p.decided_at
    return p.created_at


async def decide(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    kind: ProposalKind,
    proposal_id: uuid.UUID,
    verdict: Verdict,
    expected_version: int | None = None,
    link_kind: str | None = None,
) -> None:
    await _adapter(kind).decide(
        session,
        org_id=org_id,
        actor_id=actor_id,
        proposal_id=proposal_id,
        verdict=verdict,
        expected_version=expected_version,
        link_kind=link_kind,
    )


async def reopen(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    kind: ProposalKind,
    proposal_id: uuid.UUID,
) -> None:
    await _adapter(kind).reopen(session, org_id=org_id, actor_id=actor_id, proposal_id=proposal_id)


__all__ = [
    "Adapter",
    "Cursor",
    "Listing",
    "NoteRef",
    "Proposal",
    "ProposalKind",
    "Verdict",
    "adapters",
    "decide",
    "list_proposals",
    "reopen",
    "sort_key",
]
