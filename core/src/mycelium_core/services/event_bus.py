"""ADR-0036 event bus: emit + read the ``event_outbox``.

The single write path onto the coordinated graph substrate. Every emit
writes one ``event_outbox`` row in the CALLER's transaction (so the bus
event and the originating mutation commit or roll back together); the
deferred DB trigger ``pg_notify('mycelium.event', id)`` fires at COMMIT so a
subscriber can pull the row by id. Two guards protect autonomous actors:

  * a per-actor volume quota on write events (``propose``/``commit``),
    raising :class:`QuotaExceededError` (429) past an agent's cap
    (anti-runaway, c19b5489);
  * the ``is_inert`` gate on agent commits, enforced by the caller before
    it mutates (ADR-0036 amendment): an agent may not commit over a live
    note.

``record_classification_decision`` is the garden_classify ⇄ bus mapping
(ADR-0036 §Mapping): a synthetic ``propose`` chained to a ``commit`` or
``reject`` so every apply lands an audit chain even though suggestions are
surfaced on a pure-read endpoint (no propose event at surface time).
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Literal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.config import get_settings
from mycelium_core.errors import QuotaExceededError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.event_outbox import EventOutbox

ActorKind = Literal["human", "agent", "system"]
EventKind = Literal["read", "propose", "commit", "reject", "snapshot"]

_IDEMPOTENCY_WINDOW = datetime.timedelta(hours=24)
# Write events that count against an agent's anti-runaway quota.
_WRITE_KINDS: frozenset[str] = frozenset({"propose", "commit"})

# The anti-runaway ceilings on autonomous AGENT actors (c19b5489) live in
# Settings, beside the other rate limits. They used to be constants here
# with a per-EXECUTOR override on top, and that override was unreachable
# by construction: the lookup matched ``Executor.user_id == actor_id``,
# while ``create_executor`` forces ``user_id=None`` for every kind other
# than human and the model says so in writing ("NULL for llm_agent"). No
# llm_agent row could ever be found. The only row that COULD match was
# the caller's HUMAN executor -- exactly the actor class this function
# declares it does not cap -- so the day somebody filled those columns in
# by hand, the cap would have bitten the human and left the agent free
# (task e0738a4f).
#
# Removed rather than repaired, because Executor is a DISPATCH-side entity
# and is not in scope at emit time: ``emit_event`` is handed
# (actor_id, actor_kind) where actor_id is a users.id, and its callers --
# garden_review, garden_classify -- have no notion of an executor. If a
# genuine per-agent quota is wanted later, the key is the identity the bus
# already has on the session (``agent_tokens.assistant_id``, readable as
# ``app.current_actor_subject``), not a table with no FK to either.

# Map the fine-grained session ``app.current_actor_kind`` GUC (the same
# one audit.log reads) to the coarse bus actor class.
_COARSE: dict[str, ActorKind] = {
    "human_direct": "human",
    "human_api": "human",
    "human_telegram": "human",
    "agent_run": "agent",
    "mcp_token": "agent",
    "system": "system",
}


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def coarse_actor_kind(raw: str | None) -> ActorKind:
    """Coarsen a session actor_kind GUC value to human|agent|system.
    Unknown/empty defaults to ``human`` (the API/SPA default actor)."""
    return _COARSE.get((raw or "").strip(), "human")


async def session_actor_kind(session: AsyncSession) -> ActorKind:
    """The coarse actor class of the current session, from the GUC set by
    ``db.tenant_session`` / ``db.admin_session`` (same source as audit.log)."""
    raw = (
        await session.execute(text("SELECT current_setting('app.current_actor_kind', true)"))
    ).scalar()
    return coarse_actor_kind(raw)


async def _count_events_since(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, since: datetime.datetime
) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(EventOutbox)
                .where(
                    EventOutbox.org_id == org_id,
                    EventOutbox.actor_id == actor_id,
                    EventOutbox.ts >= since,
                )
            )
        ).scalar_one()
    )


async def _enforce_quota(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    actor_kind: ActorKind,
    now: datetime.datetime,
) -> None:
    """Anti-runaway cap on autonomous AGENT write events. Humans (UI) and
    system jobs (autonomous_budget governs them) are not capped here.

    One ceiling per window, from Settings, the same for every agent actor.
    There is no per-actor override: the one that used to be here could not
    be reached, and the note above says why."""
    if actor_kind != "agent":
        return
    settings = get_settings()
    per_min = settings.agent_event_quota_per_min
    per_day = settings.agent_event_quota_per_day
    if per_min > 0:
        n = await _count_events_since(
            session, org_id=org_id, actor_id=actor_id, since=now - datetime.timedelta(minutes=1)
        )
        if n >= per_min:
            raise QuotaExceededError(
                MessageCode.EVENT_QUOTA_EXCEEDED, limit=per_min, window="minute"
            )
    if per_day > 0:
        n = await _count_events_since(
            session, org_id=org_id, actor_id=actor_id, since=now - datetime.timedelta(days=1)
        )
        if n >= per_day:
            raise QuotaExceededError(MessageCode.EVENT_QUOTA_EXCEEDED, limit=per_day, window="day")


async def emit_event(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    actor_kind: ActorKind,
    kind: EventKind,
    payload: dict[str, Any],
    node_kind: str | None = None,
    node_id: uuid.UUID | None = None,
    parent_event_id: uuid.UUID | None = None,
    payload_schema_version: int = 1,
    idempotency_key: str | None = None,
    applied_state: str | None = None,
    applied_at: datetime.datetime | None = None,
    now: datetime.datetime | None = None,
) -> EventOutbox:
    """Write one outbox row in the caller's transaction. Idempotent on
    ``idempotency_key`` inside a 24h window (a retry returns the existing
    row); ``propose``/``commit`` count against the agent quota."""
    now = now or _utcnow()
    if idempotency_key:
        existing = (
            await session.execute(
                select(EventOutbox)
                .where(
                    EventOutbox.org_id == org_id,
                    EventOutbox.idempotency_key == idempotency_key,
                    EventOutbox.ts >= now - _IDEMPOTENCY_WINDOW,
                )
                .order_by(EventOutbox.ts.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
    if kind in _WRITE_KINDS:
        await _enforce_quota(
            session, org_id=org_id, actor_id=actor_id, actor_kind=actor_kind, now=now
        )
    event = EventOutbox(
        org_id=org_id,
        actor_id=actor_id,
        actor_kind=actor_kind,
        kind=kind,
        node_kind=node_kind,
        node_id=node_id,
        parent_event_id=parent_event_id,
        payload=payload,
        payload_schema_version=payload_schema_version,
        idempotency_key=idempotency_key,
        applied_state=applied_state,
        applied_at=applied_at,
    )
    session.add(event)
    await session.flush()
    return event


async def record_classification_decision(
    session: AsyncSession,
    *,
    actor_kind: ActorKind,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    node_id: uuid.UUID,
    suggestion_type: str,
    suggestion_value: dict[str, Any],
    action: str,
    model_version: str,
    signals_snapshot: dict[str, Any],
    override_value: dict[str, Any] | None = None,
    now: datetime.datetime | None = None,
) -> tuple[EventOutbox, EventOutbox]:
    """The garden_classify ⇄ bus mapping (ADR-0036 §Mapping): a synthetic
    ``propose`` chained to the decision (``commit`` for accept/override/
    auto, ``reject`` for reject/ignore), both in the caller's transaction
    so the bus and the ``classification_feedback`` row never disagree.
    Returns ``(propose, decision)``."""
    now = now or _utcnow()
    committed = action in ("accept", "override", "auto")
    base = {
        "suggestion_type": suggestion_type,
        "suggestion_value": suggestion_value,
        "model_version": model_version,
        "signals_snapshot": signals_snapshot,
    }
    propose = await emit_event(
        session,
        org_id=org_id,
        actor_id=actor_id,
        actor_kind=actor_kind,
        kind="propose",
        node_kind="note",
        node_id=node_id,
        payload=base,
        applied_state="committed" if committed else "rejected",
        applied_at=now,
        now=now,
    )
    decision_payload: dict[str, Any] = {**base, "action": action}
    if override_value is not None:
        decision_payload["override_value"] = override_value
    if not committed:
        decision_payload["soft"] = action == "ignore"
    decision = await emit_event(
        session,
        org_id=org_id,
        actor_id=actor_id,
        actor_kind=actor_kind,
        kind="commit" if committed else "reject",
        node_kind="note",
        node_id=node_id,
        parent_event_id=propose.id,
        payload=decision_payload,
        applied_state="committed" if committed else "rejected",
        applied_at=now,
        now=now,
    )
    return propose, decision


async def recent_events(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    limit: int = 100,
    since: datetime.datetime | None = None,
) -> list[EventOutbox]:
    """The workspace event stream, newest first (audit panel / replay).
    RLS scopes it to the org; ``since`` supports replay-from-cursor."""
    q = select(EventOutbox).where(EventOutbox.org_id == org_id)
    if since is not None:
        q = q.where(EventOutbox.ts >= since)
    q = q.order_by(EventOutbox.ts.desc()).limit(limit)
    return list((await session.execute(q)).scalars())
