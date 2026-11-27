"""Open, report on, reconcile, close and read the sessions a person steers
(docs/adr/0078).

The model module says what a session is and why it is not a worker. This
module is the four writes a host makes and the one read a person makes, and
the shape of each is decided by a failure it prevents:

:func:`open_session` is idempotent on the host's own id, per credential, and
reopens a row it finds closed. A resumed Claude Code session, or a retry
after a lost reply, finds the session it was; the alternative is two rows
for one terminal and the page counting both.

:func:`report` keeps ``attention_since`` when the state did not change. A
``working`` repeated on every tool call must not reset "working for 40 min"
to "working for 2 s".

:func:`reconcile` is the only thing that tells waiting from dead. A session
that waits for its person sends nothing, and neither does one whose terminal
was killed; the server cannot tell the two apart from silence, and must not
guess. The machine can: its host lists the sessions that are alive. So the
host reports that list, and the sessions of that machine and credential that
are not on it are closed, the rest confirmed.

:func:`agent_workers.open_worker` is where the join key is spent: a worker
joins the session the key was issued for. It lives there, not here, because
it is a worker's act and this module already depends on that one.

:func:`overview` is the read, assembled once here so the CLI and the web
group, order and word the same sessions the same way.
"""

from __future__ import annotations

import datetime as dt
import secrets
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal, get_args

from sqlalchemy import and_, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import ForbiddenError, NotFoundError, UnprocessableError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.agent_session import (
    AgentSession,
    Attention,
    BlockedOn,
    SessionAgent,
    SessionCloseReason,
    hash_join_key,
)
from mycelium_core.models.agent_session_message import AgentSessionMessage, MessageState
from mycelium_core.models.agent_worker import AgentWorker
from mycelium_core.models.membership import Role
from mycelium_core.models.task import Task
from mycelium_core.models.task_lease import TaskLease
from mycelium_core.services import agent_workers as workers_svc
from mycelium_core.services import audit
from mycelium_core.services import caller as caller_svc
from mycelium_core.services.rbac import require_role

#: A session that says it is working and has not been heard from for this
#: long is shown as gone quiet. Only ``working``: a session waiting for its
#: person sends nothing by design, and its silence says nothing. Fifteen
#: minutes because a single tool call (a test suite, a build) reports only
#: when it ends, and the suites of the projects this serves run for minutes;
#: shorter would mark the normal case as suspect.
QUIET_AFTER = dt.timedelta(minutes=15)

#: How recently a session must have been heard from for :func:`reconcile`
#: to leave it open although the host did not list it. The host reads its
#: list before the call reaches the server, and a session that opened in
#: between is not on the list but is alive. A margin on the server's own
#: clock rather than a time the host sends, so a host whose clock is off
#: cannot close what it should not. The cost: a session that died less than
#: this long ago survives until the next reconcile.
RECONCILE_GRACE = dt.timedelta(minutes=2)

_KEY_BYTES = 32


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _clip(value: str | None, n: int) -> str | None:
    value = (value or "").strip()
    return value[:n] or None


async def _caller_binding(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID | None]:
    """The calling token and the identity behind it, or a refusal.

    A session is opened by a host's hooks, which run with a credential of
    their own. A person's browser has none to bind the row to, and a model
    without a token would be bound to the person; both are refused, the same
    way ``open_worker`` refuses them."""
    caller = await caller_svc.current(session)
    if caller.token_id is None:
        raise ForbiddenError(MessageCode.SESSION_NEEDS_CREDENTIAL)
    identity_id = await workers_svc.caller_identity(
        session, org_id=org_id, actor_id=actor_id, token_id=caller.token_id
    )
    return caller.token_id, identity_id


async def own_open(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, session_id: uuid.UUID
) -> AgentSession:
    """An open session this credential opened, locked for the write, or not
    found. Another credential's session is answered as absent: reporting on
    it would put words in a session that did not say them."""
    token_id, _ = await _caller_binding(session, org_id=org_id, actor_id=actor_id)
    row = (
        await session.execute(
            select(AgentSession)
            .where(
                AgentSession.id == session_id,
                AgentSession.org_id == org_id,
                AgentSession.opened_by_user_id == actor_id,
                AgentSession.opened_by_token_id == token_id,
                AgentSession.closed_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(MessageCode.SESSION_NOT_FOUND)
    return row


async def open_session(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    agent: SessionAgent,
    host_ref: str,
    project: str | None = None,
    machine: str | None = None,
    title: str | None = None,
    join_key: str | None = None,
    accepts_messages: bool = False,
) -> tuple[AgentSession, str]:
    """Open the session the host calls ``host_ref``, or find the one this
    credential already opened under it, and return it with its join key.

    ``join_key`` is the key the host was given last time, if it kept it. It
    is kept when it matches: the agent inside the session was handed that
    key in its context at the start, and a reopening that replaced it would
    make that context stale while the session is the same one. Measured on
    2026-11-26, end to end: a reopening that rotated the key left the
    agent's ``worker_open`` refused. A key that does not match, or none, gets
    a new one; only the hash is stored, so a lost key cannot be returned.

    Reopening a closed row is what a resume after ``SessionEnd`` needs, and
    a session closed as idle that turns out to be alive comes back the same
    way, on its next event, with its history.

    ``accepts_messages`` is what the host says now, on every opening: a
    machine that stopped running relays stops offering a composer at its
    next session start."""
    await require_role(session, org_id, actor_id, Role.member)
    token_id, identity_id = await _caller_binding(session, org_id=org_id, actor_id=actor_id)
    ref = _clip(host_ref, 128)
    if ref is None:
        raise UnprocessableError(MessageCode.SESSION_HOST_REF_REQUIRED)
    at = _now()
    row = (
        await session.execute(
            select(AgentSession)
            .where(
                AgentSession.opened_by_token_id == token_id,
                AgentSession.agent == agent.value,
                AgentSession.host_ref == ref,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    reopened = row is not None and row.closed_at is not None
    if row is None:
        row = AgentSession(
            org_id=org_id,
            opened_by_user_id=actor_id,
            opened_by_identity_id=identity_id,
            opened_by_token_id=token_id,
            agent=agent.value,
            host_ref=ref,
            opened_at=at,
        )
        session.add(row)
    elif row.org_id != org_id:
        # One credential, one workspace: a token bound to another workspace
        # never reaches this one. Answered as absent all the same.
        raise NotFoundError(MessageCode.SESSION_NOT_FOUND)
    row.project = _clip(project, 128) or row.project
    row.machine = _clip(machine, 128) or row.machine
    row.title = _clip(title, 200) or row.title
    row.attention = Attention.YOUR_TURN.value
    row.blocked_on = None
    row.attention_since = at
    row.last_seen_at = at
    row.closed_at = None
    row.closed_reason = None
    row.accepts_messages = accepts_messages
    presented = (join_key or "").strip()
    if (
        presented
        and row.join_key_hash
        and secrets.compare_digest(row.join_key_hash, hash_join_key(presented))
    ):
        key = presented
    else:
        key = secrets.token_urlsafe(_KEY_BYTES)
        row.join_key_hash = hash_join_key(key)
    row.version = (row.version or 0) + 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="agent_session",
        entity_id=row.id,
        action="reopen" if reopened else "open",
        diff={"project": row.project or "", "machine": row.machine or ""},
    )
    return row, key


async def report(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    session_id: uuid.UUID,
    attention: Attention,
    blocked_on: BlockedOn | None = None,
    title: str | None = None,
) -> AgentSession:
    """Record what the session says about itself now.

    ``blocked_on`` goes with ``blocked`` and with nothing else, refused
    rather than dropped: a caller that sends ``working`` with a reason has a
    bug the row should not hide. Not audited: it changes on every turn, and
    the row itself is the record of the last claim."""
    if (attention is Attention.BLOCKED) != (blocked_on is not None):
        raise UnprocessableError(MessageCode.SESSION_BLOCKED_ON_MISMATCH)
    row = await own_open(session, org_id=org_id, actor_id=actor_id, session_id=session_id)
    at = _now()
    changed = row.attention != attention.value or row.blocked_on != (
        blocked_on.value if blocked_on else None
    )
    if changed:
        row.attention = attention.value
        row.blocked_on = blocked_on.value if blocked_on else None
        row.attention_since = at
    row.title = _clip(title, 200) or row.title
    row.last_seen_at = at
    row.version += 1
    await session.flush()
    return row


async def _abandon_queued(
    session: AsyncSession, session_ids: Sequence[uuid.UUID], at: dt.datetime
) -> None:
    """Every message still queued for these sessions will never be sent.

    Called in the transaction that closes them, by each of the three paths
    that close a session. Stored rather than read off the closed row, because
    a closed row can be reopened by a resume, and its new relay must not
    claim what the page already showed as not delivered (docs/adr/0079 §3).
    A ``claimed`` message is left alone: its relay may have written it
    already, and its outcome, or its silence, says what happened."""
    if not session_ids:
        return
    await session.execute(
        update(AgentSessionMessage)
        .where(
            AgentSessionMessage.session_id.in_(session_ids),
            AgentSessionMessage.state == MessageState.QUEUED.value,
        )
        .values(state=MessageState.NOT_DELIVERED.value, state_since=at)
        .execution_options(synchronize_session=False)
    )


async def close_session(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    session_id: uuid.UUID,
) -> AgentSession:
    """The host says the session ended. Its workers are left alone: they are
    bound to the agent's credential, not to this one, and what they hold is
    freed by their own close or by their deadlines, never by a session row."""
    row = await own_open(session, org_id=org_id, actor_id=actor_id, session_id=session_id)
    at = _now()
    row.closed_at = at
    row.closed_reason = SessionCloseReason.ENDED.value
    row.last_seen_at = at
    row.version += 1
    await session.flush()
    await _abandon_queued(session, [row.id], at)
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="agent_session",
        entity_id=row.id,
        action="close",
        diff={"reason": SessionCloseReason.ENDED.value},
    )
    return row


@dataclass(frozen=True, slots=True)
class Reconciled:
    confirmed: list[uuid.UUID]
    closed: list[uuid.UUID]


async def reconcile(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    agent: SessionAgent,
    machine: str,
    alive: Iterable[str],
) -> Reconciled:
    """The host on ``machine`` lists the sessions it has alive; the open
    sessions this credential opened there are confirmed if listed and closed
    as gone if not.

    Scoped to this credential AND this machine, both: a credential copied to
    a second machine must not close the first machine's sessions because the
    second does not list them. A session heard from within
    :data:`RECONCILE_GRACE` is never closed here, listed or not.
    """
    await require_role(session, org_id, actor_id, Role.member)
    token_id, _ = await _caller_binding(session, org_id=org_id, actor_id=actor_id)
    where_machine = _clip(machine, 128)
    if where_machine is None:
        raise UnprocessableError(MessageCode.SESSION_MACHINE_REQUIRED)
    refs = sorted({r for r in (_clip(x, 128) for x in alive) if r})
    at = _now()
    mine = and_(
        AgentSession.org_id == org_id,
        AgentSession.opened_by_user_id == actor_id,
        AgentSession.opened_by_token_id == token_id,
        AgentSession.agent == agent.value,
        AgentSession.machine == where_machine,
        AgentSession.closed_at.is_(None),
    )
    confirmed = (
        (
            await session.execute(
                update(AgentSession)
                .where(mine, AgentSession.host_ref.in_(refs))
                .values(last_seen_at=at)
                .returning(AgentSession.id)
                .execution_options(synchronize_session=False)
            )
        )
        .scalars()
        .all()
        if refs
        else []
    )
    gone = (
        (
            await session.execute(
                update(AgentSession)
                .where(
                    mine,
                    AgentSession.host_ref.not_in(refs) if refs else true(),
                    AgentSession.last_seen_at < at - RECONCILE_GRACE,
                )
                .values(
                    closed_at=at,
                    closed_reason=SessionCloseReason.GONE.value,
                    version=AgentSession.version + 1,
                )
                .returning(AgentSession.id)
                .execution_options(synchronize_session=False)
            )
        )
        .scalars()
        .all()
    )
    await _abandon_queued(session, gone, at)
    for sid in gone:
        await audit.log(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="agent_session",
            entity_id=sid,
            action="close",
            diff={"reason": SessionCloseReason.GONE.value},
        )
    return Reconciled(confirmed=list(confirmed), closed=list(gone))


async def close_idle(
    session: AsyncSession,
    *,
    idle_for: dt.timedelta,
    limit: int = 200,
    now: dt.datetime | None = None,
) -> list[uuid.UUID]:
    """Close the sessions nothing has been heard from for ``idle_for`` and
    that have no open worker.

    The backstop for a machine that never comes back: :func:`reconcile`
    closes what died on a machine the next time anything runs there, and a
    machine that was switched off for good runs nothing. The bound is long
    (see ``agent_session_idle_close_seconds``) because a session waiting for
    its person is silent by design, and closing it costs a wrong page until
    its next event reopens it."""
    at = now or _now()
    cutoff = at - idle_for
    open_worker = (
        select(AgentWorker.id)
        .where(AgentWorker.session_id == AgentSession.id, AgentWorker.closed_at.is_(None))
        .exists()
    )
    idle = and_(AgentSession.closed_at.is_(None), AgentSession.last_seen_at < cutoff, ~open_worker)
    candidates = (
        select(AgentSession.id)
        .where(idle)
        .order_by(AgentSession.last_seen_at.asc())
        .limit(limit)
        .scalar_subquery()
    )
    closed = (
        await session.execute(
            update(AgentSession)
            .where(AgentSession.id.in_(candidates), idle)
            .values(
                closed_at=at,
                closed_reason=SessionCloseReason.IDLE.value,
                version=AgentSession.version + 1,
            )
            .returning(AgentSession.id, AgentSession.org_id)
            .execution_options(synchronize_session=False)
        )
    ).all()
    await _abandon_queued(session, [row.id for row in closed], at)
    for row in closed:
        await audit.log(
            session,
            org_id=row.org_id,
            actor_id=None,
            entity="agent_session",
            entity_id=row.id,
            action="close",
            diff={"reason": SessionCloseReason.IDLE.value},
        )
    return [row.id for row in closed]


# ---------------------------------------------------------------------------
# The read a person makes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HeldTask:
    task_id: uuid.UUID
    title: str
    expires_at: dt.datetime


@dataclass(slots=True)
class SessionView:
    """One row of the page: a session, or a worker that joined none.

    ``group`` and ``order`` are decided here and nowhere else, so the CLI and
    the web cannot disagree about which session needs the person first."""

    session_id: uuid.UUID | None
    worker_ids: list[uuid.UUID]
    agent: str
    project: str | None
    machine: str | None
    title: str | None
    label: str | None
    attention: str | None
    blocked_on: str | None
    attention_since: dt.datetime | None
    opened_at: dt.datetime
    heard_at: dt.datetime
    group: Group
    held: list[HeldTask] = field(default_factory=list)
    #: The caller opened it, and its host delivers messages to it: what
    #: ``can_send`` is made of, with the caller being a person.
    opened_by_caller: bool = False
    accepts_messages: bool = False


#: Read top to bottom, as the questions are asked.
Group = Literal["needs_you", "working", "quiet"]
GROUPS: tuple[Group, ...] = get_args(Group)


def _group(attention: str | None, heard_at: dt.datetime, at: dt.datetime) -> Group:
    if attention in (Attention.BLOCKED.value, Attention.YOUR_TURN.value):
        return "needs_you"
    if at - heard_at > QUIET_AFTER:
        return "quiet"
    return "working"


def _sort_key(v: SessionView) -> tuple[int, int, float]:
    # Within "needs you": a blocked session cannot go on at all, a finished
    # one only waits for more, so blocked first; then whoever has waited
    # longest. Elsewhere: the sessions that report before a worker that does
    # not (it says only when it was last seen, which is the least a row can
    # say), then most recently heard first.
    rank = GROUPS.index(v.group)
    if v.group == "needs_you":
        blocked = 0 if v.attention == Attention.BLOCKED.value else 1
        return (rank, blocked, (v.attention_since or v.heard_at).timestamp())
    silent = 1 if v.session_id is None else 0
    return (rank, silent, -v.heard_at.timestamp())


async def overview(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    limit: int = 200,
    now: dt.datetime | None = None,
) -> list[SessionView]:
    """Every open session of the workspace, and every open worker that joined
    none, with the tasks each holds, grouped and ordered for a person.

    A worker without a session is listed rather than hidden: it is a session
    of a host that does not report (another MCP client, an agent started
    before its hooks were installed), and a page that left it out would be
    tidy and wrong."""
    await require_role(session, org_id, actor_id, Role.member)
    at = now or _now()
    sessions: Sequence[AgentSession] = (
        (
            await session.execute(
                select(AgentSession)
                .where(AgentSession.org_id == org_id, AgentSession.closed_at.is_(None))
                .order_by(AgentSession.last_seen_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    workers: Sequence[AgentWorker] = (
        (
            await session.execute(
                select(AgentWorker)
                .where(AgentWorker.org_id == org_id, AgentWorker.closed_at.is_(None))
                .order_by(AgentWorker.opened_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    worker_ids = [w.id for w in workers]
    held: dict[uuid.UUID, list[HeldTask]] = {}
    if worker_ids:
        rows = (
            await session.execute(
                select(TaskLease.holder_worker_id, Task.id, Task.title, TaskLease.expires_at)
                .join(Task, Task.id == TaskLease.task_id)
                .where(
                    TaskLease.holder_worker_id.in_(worker_ids),
                    TaskLease.released_at.is_(None),
                    Task.deleted_at.is_(None),
                )
                .order_by(TaskLease.acquired_at.asc())
            )
        ).all()
        for holder, task_id, title, expires_at in rows:
            held.setdefault(holder, []).append(
                HeldTask(task_id=task_id, title=title, expires_at=expires_at)
            )

    by_session: dict[uuid.UUID, list[AgentWorker]] = {}
    orphans: list[AgentWorker] = []
    open_ids = {s.id for s in sessions}
    for w in workers:
        if w.session_id is not None and w.session_id in open_ids:
            by_session.setdefault(w.session_id, []).append(w)
        else:
            orphans.append(w)

    views: list[SessionView] = []
    for s in sessions:
        mine = by_session.get(s.id, [])
        heard = max([s.last_seen_at, *[w.last_seen_at or w.opened_at for w in mine]])
        views.append(
            SessionView(
                session_id=s.id,
                worker_ids=[w.id for w in mine],
                agent=s.agent,
                project=s.project,
                machine=s.machine,
                title=s.title,
                label=next((w.label for w in mine if w.label), None),
                attention=s.attention,
                blocked_on=s.blocked_on,
                attention_since=s.attention_since,
                opened_at=s.opened_at,
                heard_at=heard,
                group=_group(s.attention, heard, at),
                held=[t for w in mine for t in held.get(w.id, [])],
                opened_by_caller=s.opened_by_user_id == actor_id,
                accepts_messages=s.accepts_messages,
            )
        )
    for w in orphans:
        heard = w.last_seen_at or w.opened_at
        views.append(
            SessionView(
                session_id=None,
                worker_ids=[w.id],
                agent="unknown",
                project=None,
                machine=None,
                title=None,
                label=w.label,
                attention=None,
                blocked_on=None,
                attention_since=None,
                opened_at=w.opened_at,
                heard_at=heard,
                # A host that does not report is never "working" on its word:
                # what can be said is when it was last seen doing something.
                group="working" if at - heard <= QUIET_AFTER else "quiet",
                held=held.get(w.id, []),
            )
        )
    views.sort(key=_sort_key)
    return views
