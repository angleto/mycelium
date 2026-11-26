"""The sessions a person steers (docs/adr/0078), against Postgres.

Each test pins a property the page depends on, and most of them a failure
the design was shaped against: a resume becoming a second row, a repeated
``working`` resetting how long a session has worked, a sibling session
joining another's row, a reconcile closing a session that opened while the
host was listing, a waiting session read as dead.

The CLI that runs a host's hooks and the agent inside the session are two
credentials; here they are two ``mcp_token`` subjects, which is what the
server sees of them.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import func, select, text

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.errors import ForbiddenError, NotFoundError, UnprocessableError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.agent_session import (
    AgentSession,
    Attention,
    BlockedOn,
    SessionAgent,
)
from mycelium_core.models.agent_worker import AgentWorker
from mycelium_core.models.user import User
from mycelium_core.services import agent_sessions as svc
from mycelium_core.services import agent_workers as workers_svc
from mycelium_core.services import task_leases as leases_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup
from mycelium_core.services.memberships import add_member

CC = SessionAgent.CLAUDE_CODE


@pytest.fixture
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="SESS")
    return r.org_id, r.user_id


async def _member_of(org: uuid.UUID, owner: uuid.UUID) -> uuid.UUID:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="Other")
    async with admin_session() as s:
        email = (await s.execute(select(User.email).where(User.id == r.user_id))).scalar_one()
    async with tenant_session(str(org), str(owner)) as s:
        await add_member(s, org_id=org, actor_id=owner, email=email, role="member")
    return r.user_id


def _as(org: uuid.UUID, user: uuid.UUID, token: str | None):  # type: ignore[no-untyped-def]  # an async context manager factory, typed by tenant_session
    if token is None:
        return tenant_session(str(org), str(user))
    return tenant_session(str(org), str(user), actor_kind="mcp_token", actor_subject_id=token)


async def _open(
    org: uuid.UUID,
    user: uuid.UUID,
    token: str,
    ref: str,
    *,
    project: str | None = "humus",
    machine: str | None = "lab",
) -> tuple[AgentSession, str]:
    async with _as(org, user, token) as s:
        return await svc.open_session(
            s,
            org_id=org,
            actor_id=user,
            agent=CC,
            host_ref=ref,
            project=project,
            machine=machine,
        )


async def _report(
    org: uuid.UUID,
    user: uuid.UUID,
    token: str,
    sid: uuid.UUID,
    attention: Attention,
    blocked_on: BlockedOn | None = None,
) -> AgentSession:
    async with _as(org, user, token) as s:
        return await svc.report(
            s,
            org_id=org,
            actor_id=user,
            session_id=sid,
            attention=attention,
            blocked_on=blocked_on,
        )


async def _worker(
    org: uuid.UUID, user: uuid.UUID, token: str, key: str | None, label: str = "w"
) -> AgentWorker:
    async with _as(org, user, token) as s:
        return await workers_svc.open_worker(
            s, org_id=org, actor_id=user, label=label, join_key=key
        )


async def _age_session(org: uuid.UUID, user: uuid.UUID, sid: uuid.UUID, by: dt.timedelta) -> None:
    """Move the session's last sighting, and its attention, into the past.
    Simulated rather than waited out."""
    async with tenant_session(str(org), str(user)) as s:
        n = (
            await s.execute(
                text(
                    "UPDATE agent_sessions SET last_seen_at = now() - make_interval(secs => :by), "
                    "attention_since = now() - make_interval(secs => :by) "
                    "WHERE id = :s RETURNING id"
                ),
                {"by": by.total_seconds(), "s": sid},
            )
        ).all()
    assert len(n) == 1


async def _row(org: uuid.UUID, user: uuid.UUID, sid: uuid.UUID) -> AgentSession:
    async with tenant_session(str(org), str(user)) as s:
        row = await s.get(AgentSession, sid)
        assert row is not None
        return row


async def _overview(org: uuid.UUID, user: uuid.UUID) -> list[svc.SessionView]:
    async with tenant_session(str(org), str(user)) as s:
        return await svc.overview(s, org_id=org, actor_id=user)


# ---------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------


async def test_a_resume_finds_the_session_it_was_and_gets_a_new_key(_embedder: None) -> None:
    org, user = await _org()
    hooks = str(uuid.uuid4())
    first, key1 = await _open(org, user, hooks, "cc-1")
    await _report(org, user, hooks, first.id, Attention.WORKING)

    again, key2 = await _open(org, user, hooks, "cc-1", project=None)

    assert again.id == first.id
    assert key1 != key2
    # A reopening starts on the person's turn: the host is at its prompt.
    assert again.attention == Attention.YOUR_TURN.value
    # A field the second opening leaves out keeps what the first said.
    assert again.project == "humus"
    agent = str(uuid.uuid4())
    with pytest.raises(NotFoundError) as exc:
        await _worker(org, user, agent, key1)
    assert exc.value.code is MessageCode.SESSION_JOIN_KEY_UNKNOWN
    assert (await _worker(org, user, agent, key2)).session_id == first.id


async def test_a_reopening_that_presents_its_key_keeps_it(_embedder: None) -> None:
    """The agent was handed the key at the start of the session. A reopening
    by the same host (the server had closed the session, the hook opens it
    again) must not make that context stale. Observed failing end to end on
    2026-11-26, before the key could be presented: the agent's worker_open
    was refused as stale."""
    org, user = await _org()
    hooks = str(uuid.uuid4())
    row, key = await _open(org, user, hooks, "cc-keep")
    async with _as(org, user, hooks) as s:
        await svc.close_session(s, org_id=org, actor_id=user, session_id=row.id)

    async with _as(org, user, hooks) as s:
        _, kept = await svc.open_session(
            s, org_id=org, actor_id=user, agent=CC, host_ref="cc-keep", join_key=key
        )
    async with _as(org, user, hooks) as s:
        _, rotated = await svc.open_session(
            s, org_id=org, actor_id=user, agent=CC, host_ref="cc-keep", join_key="guessed"
        )

    assert kept == key
    assert rotated != key
    assert (await _worker(org, user, str(uuid.uuid4()), rotated)).session_id == row.id


async def test_the_same_host_id_on_another_credential_is_another_session(
    _embedder: None,
) -> None:
    org, user = await _org()
    a, _ = await _open(org, user, str(uuid.uuid4()), "cc-same")
    b, _ = await _open(org, user, str(uuid.uuid4()), "cc-same")
    assert a.id != b.id


async def test_a_closed_session_is_reopened_by_its_host(_embedder: None) -> None:
    org, user = await _org()
    hooks = str(uuid.uuid4())
    row, _ = await _open(org, user, hooks, "cc-2")
    async with _as(org, user, hooks) as s:
        await svc.close_session(s, org_id=org, actor_id=user, session_id=row.id)

    again, _ = await _open(org, user, hooks, "cc-2")

    assert again.id == row.id
    assert again.closed_at is None and again.closed_reason is None


async def test_a_person_without_a_token_cannot_open_a_session(_embedder: None) -> None:
    org, user = await _org()
    async with _as(org, user, None) as s:
        with pytest.raises(ForbiddenError) as exc:
            await svc.open_session(s, org_id=org, actor_id=user, agent=CC, host_ref="x")
    assert exc.value.code is MessageCode.SESSION_NEEDS_CREDENTIAL


async def test_a_blank_host_id_is_refused(_embedder: None) -> None:
    org, user = await _org()
    with pytest.raises(UnprocessableError) as exc:
        await _open(org, user, str(uuid.uuid4()), "   ")
    assert exc.value.code is MessageCode.SESSION_HOST_REF_REQUIRED


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


async def test_repeating_a_state_keeps_the_time_it_was_entered(_embedder: None) -> None:
    """``PostToolUse`` reports ``working`` after every tool call. If each one
    reset ``attention_since``, "working for 40 min" would read "2 s"."""
    org, user = await _org()
    hooks = str(uuid.uuid4())
    row, _ = await _open(org, user, hooks, "cc-3")
    await _report(org, user, hooks, row.id, Attention.WORKING)
    await _age_session(org, user, row.id, dt.timedelta(minutes=40))
    entered = (await _row(org, user, row.id)).attention_since

    after = await _report(org, user, hooks, row.id, Attention.WORKING)

    assert after.attention_since == entered
    assert after.last_seen_at > entered

    blocked = await _report(org, user, hooks, row.id, Attention.BLOCKED, BlockedOn.PERMISSION)
    assert blocked.attention_since > entered
    assert blocked.blocked_on == BlockedOn.PERMISSION.value

    # Blocked on something else is a change too.
    question = await _report(org, user, hooks, row.id, Attention.BLOCKED, BlockedOn.QUESTION)
    assert question.attention_since > blocked.attention_since


@pytest.mark.parametrize(
    ("attention", "blocked_on"),
    [(Attention.BLOCKED, None), (Attention.WORKING, BlockedOn.PERMISSION)],
)
async def test_blocked_and_its_reason_go_together(
    _embedder: None, attention: Attention, blocked_on: BlockedOn | None
) -> None:
    org, user = await _org()
    hooks = str(uuid.uuid4())
    row, _ = await _open(org, user, hooks, "cc-4")
    with pytest.raises(UnprocessableError) as exc:
        await _report(org, user, hooks, row.id, attention, blocked_on)
    assert exc.value.code is MessageCode.SESSION_BLOCKED_ON_MISMATCH


async def test_only_the_credential_that_opened_a_session_reports_on_it(_embedder: None) -> None:
    org, user = await _org()
    row, _ = await _open(org, user, str(uuid.uuid4()), "cc-5")
    with pytest.raises(NotFoundError) as exc:
        await _report(org, user, str(uuid.uuid4()), row.id, Attention.WORKING)
    assert exc.value.code is MessageCode.SESSION_NOT_FOUND


async def test_a_closed_session_takes_no_report(_embedder: None) -> None:
    """Refused, so the hook knows to open it again rather than talk to a
    row nobody reads."""
    org, user = await _org()
    hooks = str(uuid.uuid4())
    row, _ = await _open(org, user, hooks, "cc-6")
    async with _as(org, user, hooks) as s:
        await svc.close_session(s, org_id=org, actor_id=user, session_id=row.id)
    with pytest.raises(NotFoundError):
        await _report(org, user, hooks, row.id, Attention.WORKING)


# ---------------------------------------------------------------------------
# Joining
# ---------------------------------------------------------------------------


async def test_the_agents_worker_joins_by_key_and_stays_bound_to_its_own_credential(
    _embedder: None,
) -> None:
    org, user = await _org()
    hooks, agent = str(uuid.uuid4()), str(uuid.uuid4())
    row, key = await _open(org, user, hooks, "cc-7")

    w = await _worker(org, user, agent, key)

    assert w.session_id == row.id
    assert str(w.opened_by_token_id) == agent
    # The hooks' credential still cannot act as the agent's worker: the
    # join added membership, not authority. Measured through the act it
    # would be used for, taking a task under that worker.
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="not yours")
    async with _as(org, user, hooks) as s:
        with pytest.raises(NotFoundError) as refused:
            await leases_svc.acquire(s, org_id=org, actor_id=user, task_id=task.id, worker_id=w.id)
    assert refused.value.code is MessageCode.WORKER_NOT_FOUND


async def test_a_refused_key_opens_no_worker(_embedder: None) -> None:
    org, user = await _org()
    agent = str(uuid.uuid4())
    async with tenant_session(str(org), str(user)) as s:
        before = (await s.execute(select(func.count()).select_from(AgentWorker))).scalar_one()
    with pytest.raises(NotFoundError) as exc:
        await _worker(org, user, agent, "not-a-key")
    assert exc.value.code is MessageCode.SESSION_JOIN_KEY_UNKNOWN
    async with tenant_session(str(org), str(user)) as s:
        after = (await s.execute(select(func.count()).select_from(AgentWorker))).scalar_one()
    assert after == before


async def test_a_key_presented_by_another_member_joins_nothing(_embedder: None) -> None:
    org, owner = await _org()
    other = await _member_of(org, owner)
    _, key = await _open(org, owner, str(uuid.uuid4()), "cc-8")
    with pytest.raises(NotFoundError):
        await _worker(org, other, str(uuid.uuid4()), key)


async def test_the_key_of_a_closed_session_is_refused(_embedder: None) -> None:
    org, user = await _org()
    hooks = str(uuid.uuid4())
    row, key = await _open(org, user, hooks, "cc-9")
    async with _as(org, user, hooks) as s:
        await svc.close_session(s, org_id=org, actor_id=user, session_id=row.id)
    with pytest.raises(NotFoundError):
        await _worker(org, user, str(uuid.uuid4()), key)


async def test_a_retried_open_that_now_carries_the_key_joins(_embedder: None) -> None:
    org, user = await _org()
    agent = str(uuid.uuid4())
    row, key = await _open(org, user, str(uuid.uuid4()), "cc-10")
    async with _as(org, user, agent) as s:
        first = await workers_svc.open_worker(s, org_id=org, actor_id=user, operation_id="op-1")
    async with _as(org, user, agent) as s:
        again = await workers_svc.open_worker(
            s, org_id=org, actor_id=user, operation_id="op-1", join_key=key
        )
    assert again.id == first.id
    assert again.session_id == row.id


# ---------------------------------------------------------------------------
# Reconciling
# ---------------------------------------------------------------------------


async def _reconcile(
    org: uuid.UUID, user: uuid.UUID, token: str, machine: str, alive: list[str]
) -> svc.Reconciled:
    async with _as(org, user, token) as s:
        return await svc.reconcile(
            s, org_id=org, actor_id=user, agent=CC, machine=machine, alive=alive
        )


async def test_reconcile_closes_what_the_machine_no_longer_lists_and_only_that(
    _embedder: None,
) -> None:
    org, user = await _org()
    hooks = str(uuid.uuid4())
    alive, _ = await _open(org, user, hooks, "alive", machine="lab")
    dead, _ = await _open(org, user, hooks, "dead", machine="lab")
    elsewhere, _ = await _open(org, user, hooks, "far", machine="laptop")
    other_cred, _ = await _open(org, user, str(uuid.uuid4()), "dead", machine="lab")
    for sid in (alive.id, dead.id, elsewhere.id, other_cred.id):
        await _age_session(org, user, sid, dt.timedelta(minutes=30))

    r = await _reconcile(org, user, hooks, "lab", ["alive"])

    assert r.closed == [dead.id]
    assert r.confirmed == [alive.id]
    assert (await _row(org, user, dead.id)).closed_reason == "gone"
    assert (await _row(org, user, elsewhere.id)).closed_at is None
    assert (await _row(org, user, other_cred.id)).closed_at is None
    confirmed = await _row(org, user, alive.id)
    assert dt.datetime.now(dt.UTC) - confirmed.last_seen_at < dt.timedelta(minutes=1)
    # Confirming that it is alive says nothing new about what it is doing.
    assert dt.datetime.now(dt.UTC) - confirmed.attention_since > dt.timedelta(minutes=29)


async def test_reconcile_never_closes_a_session_heard_from_within_the_grace(
    _embedder: None,
) -> None:
    """The host reads its list, then calls. A session that opened in between
    is alive and not on the list. Observed failing with ``RECONCILE_GRACE``
    set to zero on 2026-11-26: the session opened a second earlier was closed
    as gone."""
    org, user = await _org()
    hooks = str(uuid.uuid4())
    fresh, _ = await _open(org, user, hooks, "just-opened", machine="lab")

    r = await _reconcile(org, user, hooks, "lab", [])

    assert r.closed == []
    assert (await _row(org, user, fresh.id)).closed_at is None


async def test_reconcile_needs_a_machine(_embedder: None) -> None:
    org, user = await _org()
    with pytest.raises(UnprocessableError) as exc:
        await _reconcile(org, user, str(uuid.uuid4()), " ", [])
    assert exc.value.code is MessageCode.SESSION_MACHINE_REQUIRED


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


async def test_the_page_puts_who_needs_the_person_first(_embedder: None) -> None:
    org, user = await _org()
    hooks = str(uuid.uuid4())
    working, _ = await _open(org, user, hooks, "working", project="humus")
    await _report(org, user, hooks, working.id, Attention.WORKING)
    finished_long_ago, _ = await _open(org, user, hooks, "your-turn-old", project="kiwi")
    await _age_session(org, user, finished_long_ago.id, dt.timedelta(hours=3))
    _finished_now, _ = await _open(org, user, hooks, "your-turn-new", project="cylock")
    blocked, _ = await _open(org, user, hooks, "blocked", project="symbolum")
    await _report(org, user, hooks, blocked.id, Attention.BLOCKED, BlockedOn.PERMISSION)
    quiet, _ = await _open(org, user, hooks, "quiet", project="mycelium")
    await _report(org, user, hooks, quiet.id, Attention.WORKING)
    await _age_session(org, user, quiet.id, svc.QUIET_AFTER + dt.timedelta(minutes=1))
    # Heard from after every other: still below the sessions that report.
    await _worker(org, user, str(uuid.uuid4()), None, label="another-client")

    views = await _overview(org, user)

    assert [(v.project, v.group) for v in views] == [
        # Blocked cannot go on at all: first, whatever its age.
        ("symbolum", "needs_you"),
        # Then the finished ones, who has waited longest first. Three hours
        # of silence is waiting, not death: a waiting session sends nothing.
        ("kiwi", "needs_you"),
        ("cylock", "needs_you"),
        ("humus", "working"),
        (None, "working"),
        ("mycelium", "quiet"),
    ]


async def test_a_session_shows_the_tasks_its_agent_holds(_embedder: None) -> None:
    org, user = await _org()
    hooks, agent = str(uuid.uuid4()), str(uuid.uuid4())
    row, key = await _open(org, user, hooks, "holding")
    w = await _worker(org, user, agent, key, label="cc-humus")
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="Fix the sweep")
    async with _as(org, user, agent) as s:
        await leases_svc.acquire(s, org_id=org, actor_id=user, task_id=task.id, worker_id=w.id)

    (view,) = await _overview(org, user)

    assert view.session_id == row.id
    assert view.worker_ids == [w.id]
    assert view.label == "cc-humus"
    assert [t.title for t in view.held] == ["Fix the sweep"]


async def test_a_worker_in_no_session_is_listed_not_hidden(_embedder: None) -> None:
    org, user = await _org()
    w = await _worker(org, user, str(uuid.uuid4()), None, label="other-client")

    (view,) = await _overview(org, user)

    assert view.session_id is None
    assert view.agent == "unknown"
    assert view.worker_ids == [w.id]
    assert view.attention is None
    assert view.group == "working"


async def test_closed_sessions_are_not_on_the_page(_embedder: None) -> None:
    org, user = await _org()
    hooks = str(uuid.uuid4())
    row, _ = await _open(org, user, hooks, "gone")
    async with _as(org, user, hooks) as s:
        await svc.close_session(s, org_id=org, actor_id=user, session_id=row.id)
    assert await _overview(org, user) == []


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


async def test_the_sweep_closes_a_silent_session_only_once_no_worker_of_it_is_open(
    _embedder: None,
) -> None:
    org, user = await _org()
    hooks = str(uuid.uuid4())
    lone, _ = await _open(org, user, hooks, "lone")
    staffed, key = await _open(org, user, hooks, "staffed")
    await _worker(org, user, str(uuid.uuid4()), key)
    idle = dt.timedelta(days=3)
    for sid in (lone.id, staffed.id):
        await _age_session(org, user, sid, idle + dt.timedelta(minutes=1))

    async with tenant_session(str(org), str(user), actor_kind="system") as s:
        closed = await svc.close_idle(s, idle_for=idle)

    assert closed == [lone.id]
    assert (await _row(org, user, lone.id)).closed_reason == "idle"
    assert (await _row(org, user, staffed.id)).closed_at is None
