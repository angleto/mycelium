"""The session operations on the registry (docs/adr/0078).

Four writes an agent host makes from its hooks, through the CLI, and the one
read a person makes. The writes are REST-only: they are made by the host,
which sees the session's prompts, and never by the model inside it, which
does not; a model reporting its own attention could only say what it
believes, and the page would show that as fact. The read is on both
surfaces, so an agent can see who else is working and on what.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from mycelium_core.models.agent_session import (
    AgentSession,
    Attention,
    BlockedOn,
    SessionAgent,
    SessionCloseReason,
)
from mycelium_core.operations import Kind, OpCtx, operation
from mycelium_core.services import agent_sessions as svc
from mycelium_core.services.agent_sessions import Group

SESSIONS = "sessions"

_HOST_ONLY = (
    "written by an agent host's hooks, which see the session's prompts; the model inside "
    "the session does not, and its own word on whether it is blocked is not one to show "
    "a person as fact"
)


class SessionOut(BaseModel):
    id: uuid.UUID
    agent: SessionAgent
    project: str | None
    machine: str | None
    title: str | None
    attention: Attention
    blocked_on: BlockedOn | None
    attention_since: dt.datetime
    opened_at: dt.datetime
    last_seen_at: dt.datetime
    closed_at: dt.datetime | None
    closed_reason: SessionCloseReason | None


def _out(s: AgentSession) -> SessionOut:
    return SessionOut(
        id=s.id,
        agent=SessionAgent(s.agent),
        project=s.project,
        machine=s.machine,
        title=s.title,
        attention=Attention(s.attention),
        blocked_on=BlockedOn(s.blocked_on) if s.blocked_on else None,
        attention_since=s.attention_since,
        opened_at=s.opened_at,
        last_seen_at=s.last_seen_at,
        closed_at=s.closed_at,
        closed_reason=SessionCloseReason(s.closed_reason) if s.closed_reason else None,
    )


class SessionOpenIn(BaseModel):
    agent: SessionAgent = Field(description="Which host runs the session.")
    host_ref: str = Field(
        min_length=1,
        max_length=128,
        description="The host's own id for the session (Claude Code's session_id). Opening "
        "again with the same one, from the same credential, returns the same session.",
    )
    project: str | None = Field(
        default=None,
        max_length=128,
        description="A name for what the session works on: the repository's directory name. "
        "Never a path.",
    )
    machine: str | None = Field(
        default=None, max_length=128, description="A name for the machine it runs on."
    )
    title: str | None = Field(
        default=None, max_length=200, description="The host's name for the session, if any."
    )
    join_key: str | None = Field(
        default=None,
        max_length=128,
        description="The key a previous opening returned, if the host kept it. Kept when it "
        "matches, so the agent's context stays valid; otherwise a new one is issued.",
    )


class SessionOpenedOut(BaseModel):
    session: SessionOut
    join_key: str = Field(
        description="Shown once. Give it to the agent inside the session, which passes it to "
        "worker_open so its worker joins this session."
    )


@operation(
    "sessions.open",
    domain=SESSIONS,
    kind=Kind.WRITE,
    access="tasks:write",
    params=SessionOpenIn,
    result=SessionOpenedOut,
    only="rest",
    why=_HOST_ONLY,
)
async def open_(ctx: OpCtx, p: SessionOpenIn) -> SessionOpenedOut:
    """Open the session an agent host calls ``host_ref``, or find the one
    this credential opened under it (reopening it if it was closed), and
    return it with its join key: the one presented, if it matches, else a
    new one. Its attention starts as your_turn."""
    row, key = await svc.open_session(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.actor_id,
        agent=p.agent,
        host_ref=p.host_ref,
        project=p.project,
        machine=p.machine,
        title=p.title,
        join_key=p.join_key,
    )
    return SessionOpenedOut(session=_out(row), join_key=key)


class SessionReportIn(BaseModel):
    session_id: uuid.UUID
    attention: Attention = Field(
        description="working; blocked (a prompt is open and it cannot go on); your_turn (it "
        "finished and waits for the next instruction)."
    )
    blocked_on: BlockedOn | None = Field(
        default=None, description="With blocked, and only with it: permission or question."
    )
    title: str | None = Field(default=None, max_length=200)


@operation(
    "sessions.report",
    domain=SESSIONS,
    kind=Kind.WRITE,
    access="tasks:write",
    params=SessionReportIn,
    result=SessionOut,
    only="rest",
    why=_HOST_ONLY,
)
async def report(ctx: OpCtx, p: SessionReportIn) -> SessionOut:
    """Record whether a session needs its person now. Only the credential
    that opened the session may report on it. Repeating the state it is
    already in keeps the time it entered it."""
    row = await svc.report(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.actor_id,
        session_id=p.session_id,
        attention=p.attention,
        blocked_on=p.blocked_on,
        title=p.title,
    )
    return _out(row)


class SessionReconcileIn(BaseModel):
    agent: SessionAgent
    machine: str = Field(min_length=1, max_length=128)
    alive: list[str] = Field(
        max_length=1000,
        description="The host ids of every session the host lists as alive on this machine.",
    )


class SessionReconcileOut(BaseModel):
    confirmed: list[uuid.UUID]
    closed: list[uuid.UUID]


@operation(
    "sessions.reconcile",
    domain=SESSIONS,
    kind=Kind.WRITE,
    access="tasks:write",
    params=SessionReconcileIn,
    result=SessionReconcileOut,
    only="rest",
    why=_HOST_ONLY,
)
async def reconcile(ctx: OpCtx, p: SessionReconcileIn) -> SessionReconcileOut:
    """Say which sessions are alive on a machine: the open sessions this
    credential opened there are confirmed if listed and closed as gone if
    not. A session heard from in the last two minutes is never closed."""
    r = await svc.reconcile(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.actor_id,
        agent=p.agent,
        machine=p.machine,
        alive=p.alive,
    )
    return SessionReconcileOut(confirmed=r.confirmed, closed=r.closed)


class SessionCloseIn(BaseModel):
    session_id: uuid.UUID


@operation(
    "sessions.close",
    domain=SESSIONS,
    kind=Kind.WRITE,
    access="tasks:write",
    params=SessionCloseIn,
    result=SessionOut,
    only="rest",
    why=_HOST_ONLY,
)
async def close(ctx: OpCtx, p: SessionCloseIn) -> SessionOut:
    """The host says the session ended. Its workers, and what they hold,
    are left to their own close and deadlines."""
    row = await svc.close_session(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.actor_id, session_id=p.session_id
    )
    return _out(row)


class HeldTaskOut(BaseModel):
    task_id: uuid.UUID
    title: str
    expires_at: dt.datetime


class SessionRowOut(BaseModel):
    session_id: uuid.UUID | None = Field(
        description="None for a worker that joined no session: a host that does not report."
    )
    worker_ids: list[uuid.UUID]
    agent: Literal["claude_code", "unknown"]
    project: str | None
    machine: str | None
    title: str | None
    label: str | None = Field(description="What the session's worker calls itself, if anything.")
    attention: Attention | None
    blocked_on: BlockedOn | None
    attention_since: dt.datetime | None
    opened_at: dt.datetime
    heard_at: dt.datetime = Field(
        description="The last time the session or one of its workers was heard from."
    )
    group: Group = Field(
        description="needs_you (blocked or finished), working, quiet (said it was working and "
        "has not been heard from for quiet_after_seconds)."
    )
    held: list[HeldTaskOut]


class SessionCounts(BaseModel):
    needs_you: int
    working: int
    quiet: int


class SessionsPage(BaseModel):
    sessions: list[SessionRowOut] = Field(description="Ordered: who needs the person first.")
    counts: SessionCounts
    quiet_after_seconds: int
    now: dt.datetime = Field(description="The server's clock, to measure every 'since' against.")


class SessionsListIn(BaseModel):
    limit: int = Field(default=200, ge=1, le=500)


@operation(
    "sessions.list",
    domain=SESSIONS,
    kind=Kind.READ,
    access="tasks:read",
    params=SessionsListIn,
    result=SessionsPage,
)
async def list_(ctx: OpCtx, p: SessionsListIn) -> SessionsPage:
    """The sessions open in this workspace, who needs the person first: each
    with its project, machine, whether it waits for them and since when, and
    the tasks it holds. Workers that joined no session are listed too, as
    sessions of an unknown host."""
    now = dt.datetime.now(dt.UTC)
    views = await svc.overview(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.actor_id, limit=p.limit, now=now
    )
    rows = [
        SessionRowOut(
            session_id=v.session_id,
            worker_ids=v.worker_ids,
            agent="claude_code" if v.agent == SessionAgent.CLAUDE_CODE.value else "unknown",
            project=v.project,
            machine=v.machine,
            title=v.title,
            label=v.label,
            attention=Attention(v.attention) if v.attention else None,
            blocked_on=BlockedOn(v.blocked_on) if v.blocked_on else None,
            attention_since=v.attention_since,
            opened_at=v.opened_at,
            heard_at=v.heard_at,
            group=v.group,
            held=[
                HeldTaskOut(task_id=t.task_id, title=t.title, expires_at=t.expires_at)
                for t in v.held
            ],
        )
        for v in views
    ]
    counts = SessionCounts(
        needs_you=sum(1 for r in rows if r.group == "needs_you"),
        working=sum(1 for r in rows if r.group == "working"),
        quiet=sum(1 for r in rows if r.group == "quiet"),
    )
    return SessionsPage(
        sessions=rows,
        counts=counts,
        quiet_after_seconds=int(svc.QUIET_AFTER.total_seconds()),
        now=now,
    )
