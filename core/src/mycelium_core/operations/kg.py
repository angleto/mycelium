"""The knowledge graph's review operations (ADR-0068, ADR-0071).

A fact a model extracted on nobody's request is born ``proposed`` and is
withheld from every read until a person decides. Before these operations the
decision existed only as a service function nothing could reach, so a
proposed fact stayed invisible forever. Reading the inbox costs ``kg:read``;
approving or rejecting is a person's act (``HUMAN_ONLY``), like approving a
generated note.

The graph's older tools (``kg_extract``, ``kg_entities``, ``kg_neighbors``)
are still hand-written MCP tools in ``mycelium_mcp.server`` with no REST
sibling; moving them here is a separate step, not part of this one.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from pydantic import BaseModel, Field

from mycelium_core.mcp_scopes import HUMAN_ONLY
from mycelium_core.models.kg import KgEdge
from mycelium_core.operations import Kind, OpCtx, operation, paging
from mycelium_core.services import kg as kg_svc

KG = "kg"


class KgEntityRef(BaseModel):
    id: uuid.UUID
    name: str
    type: str


class KgProposedFact(BaseModel):
    """One fact awaiting a person's decision. ``origin_model_id`` names the
    model that read it and ``source_note_id`` the note it was read from, so
    the reviewer can check the claim against its source before deciding."""

    edge_id: uuid.UUID
    subject: KgEntityRef
    predicate: str
    object: KgEntityRef
    valid_from: datetime.datetime | None = None
    valid_to: datetime.datetime | None = None
    confidence: Decimal | None = None
    origin_model_id: str | None = None
    source_note_id: uuid.UUID | None = None
    source_note_title: str | None = None
    created_at: datetime.datetime
    # Echo as ``expected_version`` on approve or reject.
    version: int


class KgReviewPendingIn(BaseModel):
    limit: int = Field(50, ge=1, le=200)
    cursor: str | None = Field(
        None, description="The next_cursor of the previous page, for the next one."
    )


class KgReviewPendingPage(BaseModel):
    """One page of proposed facts, newest first. ``next_cursor`` is null on
    the last page."""

    items: list[KgProposedFact]
    next_cursor: str | None = None
    truncated: bool = False


class KgReviewActionIn(BaseModel):
    edge_id: uuid.UUID
    expected_version: int | None = Field(
        None,
        description=(
            "The version read from the pending listing; the action fails with "
            "stale_version if the fact changed since."
        ),
    )


class KgReviewActionOut(BaseModel):
    edge_id: uuid.UUID
    review_state: str  # 'approved' or 'rejected'
    version: int


@operation(
    "kg.review.pending",
    domain=KG,
    kind=Kind.READ,
    access="kg:read",
    params=KgReviewPendingIn,
    result=KgReviewPendingPage,
)
async def review_pending(ctx: OpCtx, p: KgReviewPendingIn) -> KgReviewPendingPage:
    """The knowledge-graph review inbox (ADR-0071): facts a model extracted
    from the notes on its own, newest first, each with the model that read
    it, the note it came from and the ``version`` to echo when deciding. A
    proposed fact appears here and nowhere else until a person approves it.
    Pass ``next_cursor`` back as ``cursor`` for the next page."""
    after: tuple[datetime.datetime, uuid.UUID] | None = None
    if p.cursor:
        try:
            created_raw, id_raw = paging.decode_cursor(p.cursor)
            after = (datetime.datetime.fromisoformat(created_raw), uuid.UUID(id_raw))
        except (ValueError, TypeError) as exc:
            raise paging.invalid("kg.review.pending", "cursor: not one this list issued") from exc
    rows = await kg_svc.list_proposed_facts(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.actor_id, limit=p.limit + 1, after=after
    )
    items, next_cursor, truncated = paging.page(
        rows, p.limit, key=lambda f: [f.created_at, f.edge_id]
    )
    return KgReviewPendingPage(
        items=[
            KgProposedFact(
                edge_id=f.edge_id,
                subject=KgEntityRef(id=f.subject_id, name=f.subject_name, type=f.subject_type),
                predicate=f.predicate,
                object=KgEntityRef(id=f.object_id, name=f.object_name, type=f.object_type),
                valid_from=f.valid_from,
                valid_to=f.valid_to,
                confidence=f.confidence,
                origin_model_id=f.origin_model_id,
                source_note_id=f.source_note_id,
                source_note_title=f.source_note_title,
                created_at=f.created_at,
                version=f.version,
            )
            for f in items
        ],
        next_cursor=next_cursor,
        truncated=truncated,
    )


def _action_out(edge: KgEdge) -> KgReviewActionOut:
    return KgReviewActionOut(
        edge_id=edge.id, review_state=edge.review_state or "", version=edge.version
    )


@operation(
    "kg.review.approve",
    domain=KG,
    kind=Kind.WRITE,
    access=HUMAN_ONLY,
    params=KgReviewActionIn,
    result=KgReviewActionOut,
)
async def review_approve(ctx: OpCtx, p: KgReviewActionIn) -> KgReviewActionOut:
    """Approve a proposed knowledge-graph fact (ADR-0071): it becomes
    effective and enters kg_neighbors and every other read of the graph. A
    person's act: a credential carrying a scope list cannot call it. Audited,
    idempotent; ``expected_version`` from the pending listing."""
    edge = await kg_svc.approve_fact(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.actor_id,
        edge_id=p.edge_id,
        expected_version=p.expected_version,
    )
    return _action_out(edge)


@operation(
    "kg.review.reject",
    domain=KG,
    kind=Kind.WRITE,
    access=HUMAN_ONLY,
    params=KgReviewActionIn,
    result=KgReviewActionOut,
)
async def review_reject(ctx: OpCtx, p: KgReviewActionIn) -> KgReviewActionOut:
    """Reject a proposed knowledge-graph fact (ADR-0071): it never becomes
    effective, the row is kept as frozen history, and the autonomous pass
    does not propose the same triple again. A person's act: a credential
    carrying a scope list cannot call it. Audited, idempotent;
    ``expected_version`` from the pending listing."""
    edge = await kg_svc.reject_fact(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.actor_id,
        edge_id=p.edge_id,
        expected_version=p.expected_version,
    )
    return _action_out(edge)
