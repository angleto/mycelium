"""Note edit-conflict detection + adjudication open (docs/adr/0029 P4).

The mature stage of a note is the bloom: it is the moment when
agents and humans are most likely to disagree on the direction of an
edit. The adjudication framework (ADR-0027) was built exactly to
resolve that kind of multi-actor convergence problem; this module
wires the two together for the note ecosystem.

Detection (best-effort, not transactionally strict): an edit to a
``mature`` note from an identity that differs from the previous
recent editor (within ``conflict_window_seconds``) opens an
adjudication with strategy ``human_in_loop``. The owner of the
note (resolved via memberships when the note has no owner column of
its own) is the escalation target; they resolve the adjudication
which records the canonical next direction.

A future iteration may switch to ``DebateStrategy`` when two agents
collide; for M1 of P4 the human-in-loop strategy is the safe default
and surfaces the conflict to the workspace owner. Sintesi automatica
(rejected direction stored as a ``replies_to`` note) is out of scope
for this commit; the adjudication outcome lives on its own timeline
and the user merges manually.

This module is opt-in: callers (the notes update path) invoke it
explicitly. There is no implicit trigger at the DB layer.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.models.activity_log import ActivityLog
from flow_core.models.note import Note, NoteMaturity
from flow_core.models.organization import Organization
from flow_core.services import adjudication as adj_svc

_DEFAULT_CONFLICT_WINDOW_SECONDS = 60


async def _recent_editor_identity(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_id: uuid.UUID,
    since: dt.datetime,
) -> tuple[uuid.UUID | None, str | None]:
    """Return ``(actor_subject_id, actor_kind)`` of the most recent
    write on this note since ``since``, looking back through the
    activity log. ``actor_subject_id`` is the agent_run / mcp_token
    id when the actor was non-human; the kind disambiguates."""
    stmt = (
        select(ActivityLog.actor_subject_id, ActivityLog.actor_kind)
        .where(
            ActivityLog.org_id == org_id,
            ActivityLog.entity == "note",
            ActivityLog.entity_id == note_id,
            ActivityLog.action.in_(("update", "set_maturity")),
            ActivityLog.ts >= since,
        )
        .order_by(ActivityLog.ts.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None, None
    return row[0], row[1]


async def detect_and_open_conflict(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    proposed_body: str | None,
    current_actor_kind: str,
    current_actor_subject_id: uuid.UUID | None,
    conflict_window_seconds: int = _DEFAULT_CONFLICT_WINDOW_SECONDS,
    now: dt.datetime | None = None,
) -> uuid.UUID | None:
    """Open a human-in-loop adjudication if and only if:

    1. the note exists and is ``maturity='mature'`` and not
       transplanted (``promoted_at IS NULL``);
    2. a previous write within ``conflict_window_seconds`` was made
       by a *different* actor (kind, subject_id) than the current
       caller -- two agents stepping on each other, or an agent
       overwriting a human's edit.

    Returns the new adjudication's id, or ``None`` when no conflict
    was detected (the regular update proceeds untouched).
    """
    note = (
        await session.execute(
            select(Note).where(
                Note.id == note_id,
                Note.org_id == org_id,
                Note.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if note is None:
        return None
    if note.maturity != NoteMaturity.mature.value:
        return None
    if note.promoted_at is not None:
        return None

    now = now or dt.datetime.now(dt.UTC)
    since = now - dt.timedelta(seconds=max(1, conflict_window_seconds))
    prev_subject, prev_kind = await _recent_editor_identity(
        session, org_id=org_id, note_id=note_id, since=since
    )
    if prev_kind is None:
        return None
    # Same caller editing twice in a row is not a conflict.
    same_kind = prev_kind == current_actor_kind
    same_subject = prev_subject == current_actor_subject_id
    if same_kind and same_subject:
        return None
    # If both are "human_direct" with no subject_id (the SPA case),
    # treat as the same caller too -- multi-tab edits by the owner
    # are routine, not a conflict signal.
    if prev_kind == "human_direct" and current_actor_kind == "human_direct":
        return None

    # Open the adjudication. ``human_in_loop`` is the safe default
    # for M1; future iterations may select ``debate`` via the policy
    # router when more than one agent is involved.
    question = (
        f"Edit conflict on mature note {note_id}: a {current_actor_kind} "
        f"editor wants to overwrite a recent {prev_kind} edit. "
        "Human owner: review and resolve."
    )
    context: dict[str, Any] = {
        "note_id": str(note_id),
        "current_actor_kind": current_actor_kind,
        "previous_actor_kind": prev_kind,
        "previous_actor_subject_id": (str(prev_subject) if prev_subject is not None else None),
        "proposed_body_preview": (proposed_body or "")[:500],
    }
    config: dict[str, Any] = {
        "reason": "note_edit_conflict",
        "human_prompt": (
            f"A {current_actor_kind} tried to edit mature note "
            f"{note_id} within {conflict_window_seconds}s of a "
            f"{prev_kind} edit. Resolve which direction wins."
        ),
    }
    adj = await adj_svc.start_adjudication(
        session,
        org_id=org_id,
        actor_id=actor_id,
        question_text=question,
        context=context,
        config=config,
        strategy_id="human_in_loop",
    )
    return adj.id


async def workspace_owner_for_note(
    session: AsyncSession, *, org_id: uuid.UUID, note_id: uuid.UUID
) -> uuid.UUID | None:
    """Resolve the human owner who should be notified for a
    conflict adjudication. Notes do not carry an ``owner_id`` of
    their own; we fall back to the workspace owner."""
    from flow_core.models.membership import Membership, Role

    _ = note_id  # the workspace owner does not depend on the note id;
    # kept in the signature for future per-note overrides.
    _ = (
        await session.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    rows = (
        (
            await session.execute(
                select(Membership)
                .where(Membership.org_id == org_id, Membership.role == Role.owner)
                .order_by(Membership.created_at, Membership.user_id)
            )
        )
        .scalars()
        .all()
    )
    return rows[0].user_id if rows else None
