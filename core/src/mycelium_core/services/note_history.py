"""What happened to a note, in order (ADR-0075, decision 5).

The garden changes notes on its own (the maturity sweep, the dormancy of a
superseded note, the humus mark a reviewed distillation sets) and through
the person's decisions. Each of these writes an activity row, but nothing
showed them to the person, so a note's state changed without a visible
reason. This reads those rows back, newest first, keyset-paged, keeping only
what can be told: the action, who (a person, an agent, the system), when,
and the before and after of the fields the garden moves. The words are the
interface's: this returns the action code, the actor kind and the values.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.models.activity_log import ActivityLog
from mycelium_core.services import notes as notes_svc

#: The fields whose before and after are told; every other key of a diff is
#: left out, so a body edit does not put the text in the history.
_TOLD_FIELDS = ("maturity", "humus_flag", "review_state", "is_archived", "protected")
#: The actions that move a note's maturity, whose diff is ``{"from", "to"}``.
_MATURITY_ACTIONS = ("set_maturity", "auto_maturity", "auto_dormant")


@dataclass(frozen=True)
class HistoryEvent:
    event_id: uuid.UUID
    at: dt.datetime
    action: str
    actor_kind: str
    #: ``{field: {"old": ..., "new": ...}}`` for the told fields only.
    changes: dict[str, dict[str, Any]]
    #: The generated note a review decision on this note was about, when the
    #: row names one (``distilled_note_id``).
    about_note_id: uuid.UUID | None


async def note_history(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_id: uuid.UUID,
    limit: int = 50,
    after: tuple[dt.datetime, uuid.UUID] | None = None,
) -> tuple[list[HistoryEvent], tuple[dt.datetime, uuid.UUID] | None]:
    """A page of the note's history and the cursor of the next one. The
    note must be one the caller can open."""
    await notes_svc.get_note(session, org_id=org_id, note_id=note_id)
    stmt = select(ActivityLog).where(
        ActivityLog.org_id == org_id,
        ActivityLog.entity == "note",
        ActivityLog.entity_id == note_id,
    )
    if after is not None:
        at, row_id = after
        stmt = stmt.where(
            or_(ActivityLog.ts < at, and_(ActivityLog.ts == at, ActivityLog.id < row_id))
        )
    rows = list(
        (
            await session.execute(
                stmt.order_by(ActivityLog.ts.desc(), ActivityLog.id.desc()).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    page = rows[:limit]
    out: list[HistoryEvent] = []
    for r in page:
        diff = r.diff or {}
        changes = {
            k: v
            for k, v in diff.items()
            if k in _TOLD_FIELDS and isinstance(v, dict) and {"old", "new"} <= v.keys()
        }
        # The maturity writers (a person's move, the sweep, the decay of a
        # superseded note) log ``{"from", "to"}`` at the top of the diff.
        if r.action in _MATURITY_ACTIONS and {"from", "to"} <= diff.keys():
            changes["maturity"] = {"old": diff["from"], "new": diff["to"]}
        about = diff.get("distilled_note_id")
        out.append(
            HistoryEvent(
                event_id=r.id,
                at=r.ts,
                action=r.action,
                actor_kind=r.actor_kind,
                changes=changes,
                about_note_id=uuid.UUID(about) if isinstance(about, str) else None,
            )
        )
    nxt = (page[-1].ts, page[-1].id) if len(rows) > limit else None
    return out, nxt


__all__ = ["HistoryEvent", "note_history"]
