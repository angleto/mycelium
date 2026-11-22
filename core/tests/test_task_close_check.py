"""A model declares work done only after a check by another worker.

The workflow said it in prose and nothing applied it: an agent could move
its own work to ``done``, and ``pull(transition_to=done)`` did it in one
call. ``tasks.set_state`` now asks ``task_leases.assert_may_close`` whenever
a move enters a ``closes_work`` state, and the class of the caller comes
from one place, ``services.caller``.

Each test opens its sessions with the kind and subject the real adapters
publish: ``mcp_token`` with the token as subject for MCP, ``human_api`` with
the token as subject for an agent token over REST, ``human_api`` with none
for a person, ``human_telegram`` for a Telegram turn.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.errors import ConflictError, ForbiddenError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.activity_log import ActivityLog
from mycelium_core.models.task_lease import LeaseRelease, TaskLease
from mycelium_core.models.workflow import WorkflowState
from mycelium_core.services import agent_workers as workers_svc
from mycelium_core.services import caller as caller_svc
from mycelium_core.services import task_leases as leases_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services import workflow as wf_svc
from mycelium_core.services.auth import signup

AGENT = {"actor_kind": "mcp_token", "actor_subject_id": str(uuid.uuid4())}
REST_AGENT = {"actor_kind": "human_api", "actor_subject_id": str(uuid.uuid4())}
PERSON = {"actor_kind": "human_api"}
TELEGRAM = {"actor_kind": "human_telegram"}


@pytest.fixture
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


class _Org:
    def __init__(self, org: uuid.UUID, user: uuid.UUID, states: dict[str, uuid.UUID]) -> None:
        self.org = org
        self.user = user
        self.states = states

    def session(self, who: dict[str, str]) -> object:
        return tenant_session(str(self.org), str(self.user), **who)  # type: ignore[arg-type]

    async def worker(self, label: str, who: dict[str, str] = AGENT) -> uuid.UUID:
        """A worker is bound to the credential that opens it, so a test that
        acts as another credential opens it as that one."""
        async with self.session(who) as s:  # type: ignore[attr-defined]
            return (
                await workers_svc.open_worker(s, org_id=self.org, actor_id=self.user, label=label)
            ).id

    async def task(self) -> uuid.UUID:
        async with self.session(PERSON) as s:  # type: ignore[attr-defined]
            return (
                await tasks_svc.create_task(s, org_id=self.org, actor_id=self.user, title="w")
            ).id

    async def version(self, task_id: uuid.UUID) -> int:
        async with self.session(PERSON) as s:  # type: ignore[attr-defined]
            return (await tasks_svc.get_task(s, org_id=self.org, task_id=task_id)).version

    async def take(self, who: dict[str, str], task_id: uuid.UUID, worker: uuid.UUID) -> None:
        async with self.session(who) as s:  # type: ignore[attr-defined]
            await leases_svc.acquire(
                s, org_id=self.org, actor_id=self.user, task_id=task_id, worker_id=worker
            )

    async def move(
        self,
        who: dict[str, str],
        task_id: uuid.UUID,
        state: str,
        worker: uuid.UUID | None = None,
    ) -> int:
        version = await self.version(task_id)
        async with self.session(who) as s:  # type: ignore[attr-defined]
            return await tasks_svc.set_state(
                s,
                org_id=self.org,
                actor_id=self.user,
                task_id=task_id,
                expected_version=version,
                state_id=self.states[state],
                worker_id=worker,
            )

    async def work_and_hand_off(self, task_id: uuid.UUID, worker: uuid.UUID) -> None:
        """A worker takes the task, does it, and passes it to the checking
        station: the handoff an author makes."""
        await self.move(PERSON, task_id, "working")
        await self.take(AGENT, task_id, worker)
        await self.move(AGENT, task_id, "checking", worker)


async def _org() -> _Org:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="CLOSE",
        )
    async with tenant_session(str(r.org_id), str(r.user_id)) as s:
        w = await wf_svc.create_workflow(
            s,
            org_id=r.org_id,
            actor_id=r.user_id,
            name=f"stations-{uuid.uuid4().hex[:8]}",
            states=[
                wf_svc.StateSpec(name="queued", ord=0, is_initial=True),
                wf_svc.StateSpec(name="working", ord=1),
                wf_svc.StateSpec(name="checking", ord=2, checks_work=True),
                # Left to the default: terminal, so it closes work.
                wf_svc.StateSpec(name="closed", ord=3, is_terminal=True),
                wf_svc.StateSpec(name="dropped", ord=4, is_terminal=True, closes_work=False),
            ],
            transitions=[
                ("queued", "working"),
                ("working", "checking"),
                ("working", "closed"),
                ("working", "dropped"),
                ("checking", "closed"),
                ("checking", "working"),
                ("queued", "dropped"),
                # The person reopens finished work.
                ("closed", "working"),
            ],
        )
        await wf_svc.set_default_workflow(s, org_id=r.org_id, actor_id=r.user_id, workflow_id=w.id)
        rows = (
            (await s.execute(select(WorkflowState).where(WorkflowState.workflow_id == w.id)))
            .scalars()
            .all()
        )
    return _Org(r.org_id, r.user_id, {row.name: row.id for row in rows})


# ----- the predicate --------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "subject", "expected"),
    [
        ("human_api", None, caller_svc.CallerClass.person),
        ("human_api", uuid.uuid4(), caller_svc.CallerClass.agent),
        ("mcp_token", uuid.uuid4(), caller_svc.CallerClass.agent),
        ("mcp_token", None, caller_svc.CallerClass.agent),
        ("agent_run", uuid.uuid4(), caller_svc.CallerClass.agent),
        ("issuer_api_key", uuid.uuid4(), caller_svc.CallerClass.agent),
        ("human_telegram", None, caller_svc.CallerClass.mediated),
        ("system", None, caller_svc.CallerClass.system),
        ("human_direct", None, caller_svc.CallerClass.system),
        ("human_direct", uuid.uuid4(), caller_svc.CallerClass.agent),
        ("something_new", None, caller_svc.CallerClass.agent),
        ("", None, caller_svc.CallerClass.agent),
        ("human_api", "not-a-uuid", caller_svc.CallerClass.agent),
    ],
)
def test_one_function_says_what_kind_of_credential_is_calling(
    kind: str, subject: object, expected: caller_svc.CallerClass
) -> None:
    assert caller_svc.classify(kind, subject).cls is expected


def test_only_an_agent_token_or_a_capability_is_a_presented_token() -> None:
    tok = uuid.uuid4()
    assert caller_svc.classify("mcp_token", tok).token_id == tok
    assert caller_svc.classify("human_api", tok).token_id == tok
    assert caller_svc.classify("agent_run", tok).token_id is None


# ----- the close guard ------------------------------------------------------


async def test_an_agent_cannot_close_work_nobody_handed_over(_embedder: None) -> None:
    """The person queued it into working; this worker took it from there and
    did it. Closing it now is closing its own work."""
    o = await _org()
    task_id = await o.task()
    w = await o.worker("author")
    await o.move(PERSON, task_id, "working")
    await o.take(AGENT, task_id, w)
    with pytest.raises(ConflictError) as exc:
        await o.move(AGENT, task_id, "closed", w)
    assert exc.value.code == MessageCode.TASK_CLOSE_REQUIRES_CHECK
    # Working is not a checking station: nothing in it is up for a check.
    assert exc.value.params == {"missing": "check_station"}


async def test_a_second_worker_closes_what_the_first_handed_off(_embedder: None) -> None:
    o = await _org()
    task_id = await o.task()
    author, checker = await o.worker("author"), await o.worker("checker")
    await o.work_and_hand_off(task_id, author)
    await o.take(AGENT, task_id, checker)
    await o.move(AGENT, task_id, "closed", checker)

    async with o.session(PERSON) as s:  # type: ignore[attr-defined]
        row = (
            await s.execute(
                select(ActivityLog)
                .where(
                    ActivityLog.entity == "task",
                    ActivityLog.entity_id == task_id,
                    ActivityLog.action == "set_state",
                )
                .order_by(ActivityLog.ts.desc(), ActivityLog.id.desc())
                .limit(1)
            )
        ).scalar_one()
    assert row.diff["check"] == {
        "checked_by_worker": str(checker),
        "handed_over_by_worker": str(author),
    }
    assert row.diff["worker_id"] == str(checker)


async def test_the_author_cannot_take_its_work_back_and_close_it(_embedder: None) -> None:
    o = await _org()
    task_id = await o.task()
    author = await o.worker("author")
    await o.work_and_hand_off(task_id, author)
    await o.take(AGENT, task_id, author)
    with pytest.raises(ConflictError) as exc:
        await o.move(AGENT, task_id, "closed", author)
    assert exc.value.code == MessageCode.LEASE_SELF_VERIFICATION


async def test_a_worker_that_handed_off_earlier_is_still_an_author(_embedder: None) -> None:
    """Sent back for rework and handed off again by someone else: the first
    author still wrote part of it, and still may not be the one to close."""
    o = await _org()
    task_id = await o.task()
    first, second = await o.worker("first"), await o.worker("second")
    await o.work_and_hand_off(task_id, first)
    await o.take(AGENT, task_id, second)
    await o.move(AGENT, task_id, "working", second)
    await o.take(AGENT, task_id, second)
    await o.move(AGENT, task_id, "checking", second)
    await o.take(AGENT, task_id, first)
    with pytest.raises(ConflictError) as exc:
        await o.move(AGENT, task_id, "closed", first)
    assert exc.value.code == MessageCode.LEASE_SELF_VERIFICATION


async def test_closing_needs_the_task_held_by_the_worker_that_closes(_embedder: None) -> None:
    o = await _org()
    task_id = await o.task()
    author, checker = await o.worker("author"), await o.worker("checker")
    await o.work_and_hand_off(task_id, author)
    # A dispatched run moves tasks it never took (it has no worker), and is
    # a model all the same.
    run = {"actor_kind": "agent_run", "actor_subject_id": str(uuid.uuid4())}
    with pytest.raises(ConflictError) as exc:
        await o.move(run, task_id, "closed")
    assert exc.value.code == MessageCode.TASK_CLOSE_REQUIRES_CHECK
    assert exc.value.params == {"missing": "lease"}
    # Held, but by somebody else's worker: still not this caller's check.
    await o.take(AGENT, task_id, checker)
    other = await o.worker("other")
    with pytest.raises(ConflictError):
        await o.move(AGENT, task_id, "closed", other)


async def test_pulling_straight_into_done_is_refused(_embedder: None) -> None:
    o = await _org()
    task_id = await o.task()
    author, checker = await o.worker("author"), await o.worker("checker")
    await o.work_and_hand_off(task_id, author)
    async with o.session(AGENT) as s:  # type: ignore[attr-defined]
        with pytest.raises(ConflictError) as exc:
            await leases_svc.pull(
                s,
                org_id=o.org,
                actor_id=o.user,
                state_id=o.states["checking"],
                worker_id=checker,
                transition_to=o.states["closed"],
            )
    assert exc.value.code == MessageCode.TASK_CLOSE_REQUIRES_CHECK


async def test_a_worker_pulling_its_work_into_checking_is_its_own_handover(
    _embedder: None,
) -> None:
    """``pull(transition_to=...)`` is a take and a move, and the move is
    recorded as the puller passing the task on, like any other handoff: the
    puller may not close it, and another worker that takes it may.

    Before, the move ran before any lease existed and recorded nobody, so
    no agent at all could close a task pulled into checking."""
    o = await _org()
    task_id = await o.task()
    w = await o.worker("puller")
    await o.move(PERSON, task_id, "working")
    async with o.session(AGENT) as s:  # type: ignore[attr-defined]
        await leases_svc.pull(
            s,
            org_id=o.org,
            actor_id=o.user,
            state_id=o.states["working"],
            worker_id=w,
            transition_to=o.states["checking"],
        )
    with pytest.raises(ConflictError) as exc:
        await o.move(AGENT, task_id, "closed", w)
    assert exc.value.code == MessageCode.LEASE_SELF_VERIFICATION

    async with o.session(PERSON) as s:  # type: ignore[attr-defined]
        released = (
            await s.execute(
                select(TaskLease.release_reason).where(
                    TaskLease.task_id == task_id, TaskLease.holder_worker_id == w
                )
            )
        ).scalars()
        assert LeaseRelease.handoff.value in set(released)

    checker = await o.worker("checker")
    async with o.session(PERSON) as s:  # type: ignore[attr-defined]
        # The puller still holds it in checking; it gives it back first.
        await leases_svc.release(
            s,
            org_id=o.org,
            actor_id=o.user,
            task_id=task_id,
            reason=LeaseRelease.explicit,
            require_holder=False,
        )
    await o.take(AGENT, task_id, checker)
    await o.move(AGENT, task_id, "closed", checker)


async def test_the_task_records_who_handed_it_over_even_without_a_lease_row(
    _embedder: None,
) -> None:
    """``tasks.handed_over_by_worker_id`` is read on its own, not only through
    the worker's handoff leases. Every path that writes it today also writes
    such a lease, so this state is built by hand: it is the one a lease row
    lost to a race between the sweep and the move would leave, and the record
    on the task must still refuse the author."""
    from sqlalchemy import update

    from mycelium_core.models.task import Task

    o = await _org()
    task_id = await o.task()
    w = await o.worker("author")
    await o.move(PERSON, task_id, "working")
    await o.move(PERSON, task_id, "checking")
    async with o.session(PERSON) as s:  # type: ignore[attr-defined]
        await s.execute(update(Task).where(Task.id == task_id).values(handed_over_by_worker_id=w))
    await o.take(AGENT, task_id, w)
    with pytest.raises(ConflictError) as exc:
        await o.move(AGENT, task_id, "closed", w)
    assert exc.value.code == MessageCode.LEASE_SELF_VERIFICATION


async def _assistant_session(o: _Org, label: str) -> tuple[dict[str, str], uuid.UUID]:
    """A session as a real assistant's MCP token: the token resolves to the
    assistant's own identity, as the adapters publish it. Returns the
    session and the assistant."""
    from mycelium_core.services import ai_assistants

    async with o.session(PERSON) as s:  # type: ignore[attr-defined]
        made = await ai_assistants.create_assistant(
            s, org_id=o.org, actor_id=o.user, label=label, scope=["tasks:read", "tasks:write"]
        )
    return await _live_token_session(o, made.assistant.id), made.assistant.id


async def _live_token_session(o: _Org, assistant_id: uuid.UUID) -> dict[str, str]:
    from mycelium_core.models.agent_token import AgentToken

    async with o.session(PERSON) as s:  # type: ignore[attr-defined]
        token_id = (
            await s.execute(
                select(AgentToken.id).where(
                    AgentToken.assistant_id == assistant_id, AgentToken.revoked_at.is_(None)
                )
            )
        ).scalar_one()
    return {"actor_kind": "mcp_token", "actor_subject_id": str(token_id)}


async def test_a_worker_answers_only_to_the_assistant_that_opened_it(_embedder: None) -> None:
    """Worker ids are visible to every member. Naming another assistant's
    worker used to be accepted, and the close recorded the caller as that
    session: the record of who checked the work could be forged."""
    o = await _org()
    task_id = await o.task()
    author = await o.worker("author")
    await o.work_and_hand_off(task_id, author)
    (alice, _), (bob, bob_id) = (
        await _assistant_session(o, "alice"),
        await _assistant_session(o, "bob"),
    )
    async with o.session(bob) as s:  # type: ignore[attr-defined]
        bobs = (await workers_svc.open_worker(s, org_id=o.org, actor_id=o.user, label="b")).id

    from mycelium_core.errors import NotFoundError
    from mycelium_core.services import ai_assistants

    with pytest.raises(NotFoundError) as exc:
        await o.take(alice, task_id, bobs)
    assert exc.value.code == MessageCode.WORKER_NOT_FOUND
    # The assistant that opened it is unaffected, and stays so when its
    # credential is replaced under the running session: the binding is to
    # the assistant's identity, not to one token.
    await o.take(bob, task_id, bobs)
    async with o.session(PERSON) as s:  # type: ignore[attr-defined]
        await ai_assistants.rotate_secret(s, org_id=o.org, actor_id=o.user, assistant_id=bob_id)
    rotated = await _live_token_session(o, bob_id)
    assert rotated != bob
    with pytest.raises(NotFoundError):
        await o.move(alice, task_id, "closed", bobs)
    await o.move(rotated, task_id, "closed", bobs)


async def test_a_handoff_from_before_a_reopen_does_not_check_the_new_work(
    _embedder: None,
) -> None:
    """The person reopens checked work; the checker takes it, redoes it and
    closes it. The first round's handoff is still on record, and it says
    nothing about the second round's work, which nobody checked."""
    o = await _org()
    task_id = await o.task()
    author, checker = await o.worker("author"), await o.worker("checker")
    await o.work_and_hand_off(task_id, author)
    await o.take(AGENT, task_id, checker)
    await o.move(AGENT, task_id, "closed", checker)
    await o.move(PERSON, task_id, "working")
    await o.take(AGENT, task_id, checker)
    with pytest.raises(ConflictError) as exc:
        await o.move(AGENT, task_id, "closed", checker)
    assert exc.value.params == {"missing": "check_station"}
    # The second round handed over by its own author is checkable again,
    # by a worker that authored neither round.
    await o.move(AGENT, task_id, "checking", checker)
    third = await o.worker("third")
    await o.take(AGENT, task_id, third)
    await o.move(AGENT, task_id, "closed", third)


async def _last_release(o: _Org, task_id: uuid.UUID) -> TaskLease:
    async with o.session(PERSON) as s:  # type: ignore[attr-defined]
        return (
            await s.execute(
                select(TaskLease)
                .where(TaskLease.task_id == task_id, TaskLease.released_at.is_not(None))
                .order_by(TaskLease.released_at.desc())
                .limit(1)
            )
        ).scalar_one()


async def test_a_move_by_someone_else_is_not_the_holder_s_handoff(_embedder: None) -> None:
    """The owner moves a task a checker holds. Recording that as the checker's
    handoff made the checker an author of work it never passed on, and gave
    any other worker a handover to close on."""
    o = await _org()
    task_id = await o.task()
    author, checker = await o.worker("author"), await o.worker("checker")
    await o.work_and_hand_off(task_id, author)
    await o.take(AGENT, task_id, checker)
    await o.move(PERSON, task_id, "working")
    assert (await _last_release(o, task_id)).release_reason == LeaseRelease.preempted.value
    # The person sends it back to checking; the checker it was taken from
    # is not an author, and this move is the person's, not a handover.
    await o.move(PERSON, task_id, "checking")
    await o.take(AGENT, task_id, checker)
    with pytest.raises(ConflictError) as exc:
        await o.move(AGENT, task_id, "closed", checker)
    assert exc.value.params == {"missing": "handoff"}


async def test_moving_a_task_off_a_dead_session_is_not_its_handoff(_embedder: None) -> None:
    o = await _org()
    task_id = await o.task()
    dead, other = await o.worker("dead"), await o.worker("other")
    await o.move(PERSON, task_id, "working")
    await o.take(AGENT, task_id, dead)
    # The session died: its deadline passed and the sweep has not come yet.
    async with o.session({"actor_kind": "system"}) as s:  # type: ignore[attr-defined]
        await s.execute(
            TaskLease.__table__.update()
            .where(TaskLease.task_id == task_id, TaskLease.released_at.is_(None))
            .values(expires_at=TaskLease.acquired_at)
        )
    await o.move(PERSON, task_id, "checking")
    assert (await _last_release(o, task_id)).release_reason == LeaseRelease.expired.value
    await o.take(AGENT, task_id, other)
    with pytest.raises(ConflictError) as exc:
        await o.move(AGENT, task_id, "closed", other)
    assert exc.value.params == {"missing": "handoff"}


async def test_work_a_person_moved_by_hand_is_closed_by_the_person(_embedder: None) -> None:
    """A person's move with no worker names nobody as the author, so it is not
    taken as a handover: the agent checks, and the person closes."""
    o = await _org()
    task_id = await o.task()
    checker = await o.worker("checker")
    await o.move(PERSON, task_id, "working")
    await o.move(PERSON, task_id, "checking")
    await o.take(AGENT, task_id, checker)
    with pytest.raises(ConflictError) as exc:
        await o.move(AGENT, task_id, "closed", checker)
    assert exc.value.params == {"missing": "handoff"}
    await o.move(PERSON, task_id, "closed")


async def test_work_a_person_handed_over_through_a_worker_is_closed_by_the_checker(
    _embedder: None,
) -> None:
    """A person who passes work into the station through a worker of their own
    is its author like any other: the work is not the model's, so the agent
    that checks it closes it."""
    o = await _org()
    task_id = await o.task()
    author = await o.worker("author", PERSON)
    checker = await o.worker("checker")
    await o.move(PERSON, task_id, "working")
    await o.take(PERSON, task_id, author)
    await o.move(PERSON, task_id, "checking", author)
    await o.take(AGENT, task_id, checker)
    await o.move(AGENT, task_id, "closed", checker)


async def test_a_person_closes_directly(_embedder: None) -> None:
    o = await _org()
    task_id = await o.task()
    await o.move(PERSON, task_id, "working")
    await o.move(PERSON, task_id, "closed")


async def test_the_server_acting_for_itself_closes_directly(_embedder: None) -> None:
    o = await _org()
    task_id = await o.task()
    await o.move({"actor_kind": "human_direct"}, task_id, "working")
    await o.move({"actor_kind": "system"}, task_id, "closed")


async def test_ending_the_work_without_doing_it_needs_no_check(_embedder: None) -> None:
    o = await _org()
    task_id = await o.task()
    w = await o.worker("triage")
    await o.take(AGENT, task_id, w)
    await o.move(AGENT, task_id, "dropped", w)


async def test_a_telegram_turn_does_not_close_work(_embedder: None) -> None:
    o = await _org()
    task_id = await o.task()
    await o.move(PERSON, task_id, "working")
    await o.move(PERSON, task_id, "checking")
    with pytest.raises(ConflictError) as exc:
        await o.move(TELEGRAM, task_id, "closed")
    assert exc.value.params == {"missing": "lease"}


async def test_an_agent_token_over_rest_is_an_agent(_embedder: None) -> None:
    """``human_api`` with a token as subject: what the REST adapter publishes
    for an agent token. It must take the lease and close only after a check,
    like the same token over MCP."""
    o = await _org()
    task_id = await o.task()
    w = await o.worker("rest", REST_AGENT)
    with pytest.raises(ConflictError) as exc:
        await o.move(REST_AGENT, task_id, "working", w)
    assert exc.value.code == MessageCode.LEASE_REQUIRED
    await o.take(REST_AGENT, task_id, w)
    await o.move(REST_AGENT, task_id, "working", w)
    await o.take(REST_AGENT, task_id, w)
    with pytest.raises(ConflictError):
        await o.move(REST_AGENT, task_id, "closed", w)


async def test_an_agent_token_over_rest_does_not_take_the_owner_exemption(
    _embedder: None,
) -> None:
    o = await _org()
    task_id = await o.task()
    holder, other = await o.worker("holder"), await o.worker("other", REST_AGENT)
    await o.take(AGENT, task_id, holder)
    with pytest.raises(ConflictError) as exc:
        await o.move(REST_AGENT, task_id, "working", other)
    assert exc.value.code == MessageCode.LEASE_HELD_BY_OTHER
    # The person whose workspace it is still unsticks it.
    await o.move(PERSON, task_id, "working")


# ----- the attribute is configuration -------------------------------------


async def test_closes_work_follows_terminal_unless_stated_and_survives_an_edit_that_omits_it(
    _embedder: None,
) -> None:
    o = await _org()
    async with o.session(PERSON) as s:  # type: ignore[attr-defined]
        rows = {
            r.name: r
            for r in (
                await s.execute(
                    select(WorkflowState).where(WorkflowState.id.in_(list(o.states.values())))
                )
            )
            .scalars()
            .all()
        }
        assert rows["closed"].closes_work is True
        assert rows["dropped"].closes_work is False
        assert rows["working"].closes_work is False
        wf_id = rows["closed"].workflow_id
        # An editor that does not know the attribute saves the workflow back.
        await wf_svc.update_workflow(
            s,
            org_id=o.org,
            actor_id=o.user,
            workflow_id=wf_id,
            name="renamed",
            states=[
                wf_svc.StateEdit(
                    id=r.id,
                    name=r.name,
                    ord=r.ord,
                    is_initial=r.is_initial,
                    is_terminal=r.is_terminal,
                )
                for r in rows.values()
            ]
            + [wf_svc.StateEdit(name="abandoned", ord=9, is_terminal=True)],
            transitions=[("queued", "working")],
        )
        after = {
            r.name: r.closes_work
            for r in (
                await s.execute(select(WorkflowState).where(WorkflowState.workflow_id == wf_id))
            )
            .scalars()
            .all()
        }
    assert after["dropped"] is False, "an edit that omitted the flag reset it"
    assert after["closed"] is True
    assert after["abandoned"] is True, "a new terminal state closes work unless stated"


async def test_a_new_workspace_is_born_with_done_closing_work(_embedder: None) -> None:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="FRESH",
        )
    async with tenant_session(str(r.org_id), str(r.user_id)) as s:
        flags = {
            st.name: st.closes_work
            for st in (await s.execute(select(WorkflowState))).scalars().all()
        }
    assert flags == {"todo": False, "in_progress": False, "done": True}


async def test_the_guard_itself_refuses_a_lease_held_by_another_worker(_embedder: None) -> None:
    """``set_state`` refuses a task held by someone else before it reaches the
    close guard, so the guard's own holder check is invisible through it. It
    is called directly here because ``pull`` skips that first check
    (``_lease_checked``) and the guard is all that stands on that path."""
    o = await _org()
    task_id = await o.task()
    author, holder, other = (
        await o.worker("author"),
        await o.worker("holder"),
        await o.worker("other"),
    )
    await o.work_and_hand_off(task_id, author)
    await o.take(AGENT, task_id, holder)
    async with o.session(AGENT) as s:  # type: ignore[attr-defined]
        with pytest.raises(ConflictError) as exc:
            await leases_svc.assert_may_close(s, task_id=task_id, worker_id=other)
        assert exc.value.params == {"missing": "lease"}
        assert await leases_svc.assert_may_close(s, task_id=task_id, worker_id=holder) == {
            "checked_by_worker": str(holder),
            "handed_over_by_worker": str(author),
        }


# ----- the other askers ----------------------------------------------------


@pytest.mark.parametrize(
    ("who", "expected"),
    [
        (REST_AGENT, "agent"),
        (AGENT, "agent"),
        (PERSON, "human"),
        (TELEGRAM, "human"),
        ({"actor_kind": "system"}, "system"),
    ],
)
async def test_the_bus_counts_an_agent_token_over_rest_as_an_agent(
    who: dict[str, str], expected: str
) -> None:
    from mycelium_core.services import event_bus

    o = await _org()
    async with o.session(who) as s:  # type: ignore[attr-defined]
        assert await event_bus.session_actor_kind(s) == expected


async def test_the_digest_marks_a_move_by_an_agent_token_over_rest_as_an_agent_s(
    _embedder: None,
) -> None:
    import datetime as dt

    from mycelium_core.services import work_digest

    o = await _org()
    by_person, by_rest_agent = await o.task(), await o.task()
    w = await o.worker("rest", REST_AGENT)
    await o.move(PERSON, by_person, "working")
    await o.take(REST_AGENT, by_rest_agent, w)
    await o.move(REST_AGENT, by_rest_agent, "working", w)
    now = dt.datetime.now(dt.UTC)
    async with o.session(PERSON) as s:  # type: ignore[attr-defined]
        d = await work_digest.digest(
            s,
            org_id=o.org,
            start_from=now - dt.timedelta(hours=1),
            start_to=now + dt.timedelta(hours=1),
        )
    moves = {
        t.task_id: [e.via_agent for e in t.events if e.kind.value == "state_changed"]
        for t in d.tasks
    }
    assert moves[by_person] == [False]
    assert moves[by_rest_agent] == [True]


# ----- the workflow is the person's -------------------------------------------


@pytest.mark.parametrize("who", [AGENT, REST_AGENT, TELEGRAM], ids=["mcp", "rest", "telegram"])
async def test_a_model_cannot_rewrite_the_workflow_that_guards_it(
    _embedder: None, who: dict[str, str]
) -> None:
    """Clearing ``closes_work`` on the closing state, or making the initial
    state a closing one, would let an agent close its own work with no
    check. Every workflow write is refused to a model, so none of them is
    the way round."""
    o = await _org()
    async with o.session(PERSON) as s:  # type: ignore[attr-defined]
        wf_id = (
            await s.execute(
                select(WorkflowState.workflow_id).where(WorkflowState.id == o.states["closed"])
            )
        ).scalar_one()
        states = (
            (await s.execute(select(WorkflowState).where(WorkflowState.workflow_id == wf_id)))
            .scalars()
            .all()
        )
        edits = [
            wf_svc.StateEdit(
                id=st.id,
                name=st.name,
                ord=st.ord,
                is_initial=st.is_initial,
                is_terminal=st.is_terminal,
                closes_work=False,
            )
            for st in states
        ]
    writes = {
        "update": lambda s: wf_svc.update_workflow(
            s,
            org_id=o.org,
            actor_id=o.user,
            workflow_id=wf_id,
            name="weakened",
            states=edits,
            transitions=[("queued", "working"), ("working", "closed")],
        ),
        "create": lambda s: wf_svc.create_workflow(
            s,
            org_id=o.org,
            actor_id=o.user,
            name="mine",
            states=[wf_svc.StateSpec(name="a", is_initial=True)],
            transitions=[],
        ),
        "delete": lambda s: wf_svc.delete_workflow(
            s, org_id=o.org, actor_id=o.user, workflow_id=wf_id
        ),
        "default": lambda s: wf_svc.set_default_workflow(
            s, org_id=o.org, actor_id=o.user, workflow_id=wf_id
        ),
        "project": lambda s: wf_svc.set_project_workflow(
            s,
            org_id=o.org,
            actor_id=o.user,
            project_tag_id=uuid.uuid4(),
            workflow_id=None,
            expected_version=1,
        ),
    }
    for name, write in writes.items():
        async with o.session(who) as s:  # type: ignore[attr-defined]
            with pytest.raises(ForbiddenError) as exc:
                await write(s)
            assert exc.value.code == MessageCode.WORKFLOW_PERSON_ONLY, name
    async with o.session(PERSON) as s:  # type: ignore[attr-defined]
        closes = (
            await s.execute(
                select(WorkflowState.closes_work).where(WorkflowState.id == o.states["closed"])
            )
        ).scalar_one()
    assert closes is True
    # The person edits it.
    async with o.session(PERSON) as s:  # type: ignore[attr-defined]
        await writes["update"](s)


def test_no_tool_lets_a_scoped_credential_write_a_workflow() -> None:
    from mycelium_core.mcp_scopes import HUMAN_ONLY, VALID_SCOPE_KEYS
    from mycelium_mcp.tool_scopes import TOOL_SCOPES

    assert "workflows:write" not in VALID_SCOPE_KEYS
    for tool in (
        "create_workflow",
        "update_workflow",
        "delete_workflow",
        "set_default_workflow",
        "set_project_workflow",
    ):
        assert TOOL_SCOPES[tool] is HUMAN_ONLY, tool


async def test_a_pull_into_a_working_state_is_not_work_handed_over_for_a_check(
    _embedder: None,
) -> None:
    """The ordinary pull, queued into working in one call, is the puller
    passing the task on, into a working state and not a checking station:
    once the puller lets go, another worker that takes the task and closes
    it from working has checked nothing."""
    o = await _org()
    task_id = await o.task()
    w1, w2 = await o.worker("w1"), await o.worker("w2")
    async with o.session(AGENT) as s:  # type: ignore[attr-defined]
        pulled, _ = await leases_svc.pull(
            s,
            org_id=o.org,
            actor_id=o.user,
            state_id=o.states["queued"],
            worker_id=w1,
            transition_to=o.states["working"],
        )
    assert pulled.id == task_id
    async with o.session(AGENT) as s:  # type: ignore[attr-defined]
        await leases_svc.release(
            s,
            org_id=o.org,
            actor_id=o.user,
            task_id=task_id,
            reason=LeaseRelease.explicit,
            worker_id=w1,
        )
    await o.take(AGENT, task_id, w2)
    with pytest.raises(ConflictError) as exc:
        await o.move(AGENT, task_id, "closed", w2)
    assert exc.value.code == MessageCode.TASK_CLOSE_REQUIRES_CHECK
    assert exc.value.params == {"missing": "check_station"}
    # Passed on for a check, the same task closes.
    await o.move(AGENT, task_id, "checking", w2)
    w3 = await o.worker("w3")
    await o.take(AGENT, task_id, w3)
    await o.move(AGENT, task_id, "closed", w3)


async def test_a_model_closes_only_its_own_worker_and_a_person_any(_embedder: None) -> None:
    """Closing a worker frees what it holds. Another assistant closing it
    freed bob's task and, after a pull, recorded bob as having handed over
    work he never passed on."""
    from mycelium_core.errors import NotFoundError

    o = await _org()
    task_id = await o.task()
    (alice, _), (bob, _) = (
        await _assistant_session(o, "alice"),
        await _assistant_session(o, "bob"),
    )
    bobs = await o.worker("b", bob)
    await o.move(PERSON, task_id, "working")
    await o.take(bob, task_id, bobs)
    async with o.session(alice) as s:  # type: ignore[attr-defined]
        with pytest.raises(NotFoundError) as exc:
            await workers_svc.close_worker(s, org_id=o.org, actor_id=o.user, worker_id=bobs)
    assert exc.value.code == MessageCode.WORKER_NOT_FOUND
    async with o.session(REST_AGENT) as s:  # type: ignore[attr-defined]
        with pytest.raises(NotFoundError):
            await workers_svc.close_worker(s, org_id=o.org, actor_id=o.user, worker_id=bobs)
    async with o.session(bob) as s:  # type: ignore[attr-defined]
        assert (await leases_svc.live_lease(s, task_id=task_id)) is not None
    # The person clears a session that is stuck, whoever opened it.
    async with o.session(PERSON) as s:  # type: ignore[attr-defined]
        _, freed = await workers_svc.close_worker(s, org_id=o.org, actor_id=o.user, worker_id=bobs)
    assert freed == [task_id]


async def test_two_bare_tokens_of_one_person_do_not_share_workers(_embedder: None) -> None:
    """A bare token (the CLI's) has no identity of its own, only the
    person's, which every other credential of theirs shares. Binding on that
    identity let one such token act under the other's worker."""
    from mycelium_core.errors import NotFoundError

    o = await _org()
    task_id = await o.task()
    token_a = {"actor_kind": "human_api", "actor_subject_id": str(uuid.uuid4())}
    token_b = {"actor_kind": "human_api", "actor_subject_id": str(uuid.uuid4())}
    a = await o.worker("a", token_a)
    await o.move(PERSON, task_id, "working")
    with pytest.raises(NotFoundError) as exc:
        await o.take(token_b, task_id, a)
    assert exc.value.code == MessageCode.WORKER_NOT_FOUND
    # Nor does a caller with no token at all, a Telegram turn for one.
    with pytest.raises(NotFoundError):
        await o.take(TELEGRAM, task_id, a)
    await o.take(token_a, task_id, a)


async def test_a_session_without_a_token_uses_only_a_worker_no_token_opened(
    _embedder: None,
) -> None:
    from mycelium_core.errors import NotFoundError

    o = await _org()
    task_id = await o.task()
    mine = await o.worker("person", PERSON)
    an_agents = await o.worker("agent", AGENT)
    await o.move(PERSON, task_id, "working")
    with pytest.raises(NotFoundError):
        await o.take(REST_AGENT, task_id, mine)
    # The other direction: no token may not speak as a session a token opened.
    for who in (PERSON, TELEGRAM):
        with pytest.raises(NotFoundError):
            await o.take(who, task_id, an_agents)
    await o.take(PERSON, task_id, mine)


async def test_a_checking_station_is_stated_and_never_ends_the_work(_embedder: None) -> None:
    """Which state checks is the workflow's to say. A terminal or closing one
    cannot: the close would accept a handoff into the state it is entering.
    An edit that leaves the attribute out keeps it."""
    from mycelium_core.errors import DomainError

    o = await _org()
    async with o.session(PERSON) as s:  # type: ignore[attr-defined]
        for bad in (
            wf_svc.StateSpec(name="end", ord=1, is_terminal=True, checks_work=True),
            wf_svc.StateSpec(name="end", ord=1, closes_work=True, checks_work=True),
        ):
            with pytest.raises(DomainError) as exc:
                await wf_svc.create_workflow(
                    s,
                    org_id=o.org,
                    actor_id=o.user,
                    name=f"bad-{uuid.uuid4().hex[:6]}",
                    states=[wf_svc.StateSpec(name="start", ord=0, is_initial=True), bad],
                    transitions=[("start", "end")],
                )
            assert exc.value.code == MessageCode.WORKFLOW_INVALID
        rows = {
            r.name: r
            for r in (
                await s.execute(
                    select(WorkflowState).where(WorkflowState.id.in_(list(o.states.values())))
                )
            ).scalars()
        }
        assert rows["checking"].checks_work is True
        assert rows["working"].checks_work is False
        wf_id = rows["checking"].workflow_id
        await wf_svc.update_workflow(
            s,
            org_id=o.org,
            actor_id=o.user,
            workflow_id=wf_id,
            name="renamed",
            states=[
                wf_svc.StateEdit(
                    id=r.id,
                    name=r.name,
                    ord=r.ord,
                    is_initial=r.is_initial,
                    is_terminal=r.is_terminal,
                )
                for r in sorted(rows.values(), key=lambda r: r.ord)
            ],
            transitions=[("queued", "working"), ("working", "checking"), ("checking", "closed")],
        )
        await s.refresh(rows["checking"])
        assert rows["checking"].checks_work is True


async def test_a_state_flagged_later_does_not_turn_an_old_pull_into_a_check(
    _embedder: None,
) -> None:
    """Whether a state is a station is read when the task enters it. Read at
    the close, a person flagging ``working`` after the fact turned the pull
    into it into work passed on for a check, and the next worker closed it."""
    o = await _org()
    task_id = await o.task()
    w1, w2 = await o.worker("w1"), await o.worker("w2")
    async with o.session(AGENT) as s:  # type: ignore[attr-defined]
        await leases_svc.pull(
            s,
            org_id=o.org,
            actor_id=o.user,
            state_id=o.states["queued"],
            worker_id=w1,
            transition_to=o.states["working"],
        )
    async with o.session(AGENT) as s:  # type: ignore[attr-defined]
        await leases_svc.release(
            s,
            org_id=o.org,
            actor_id=o.user,
            task_id=task_id,
            reason=LeaseRelease.explicit,
            worker_id=w1,
        )
    async with o.session(PERSON) as s:  # type: ignore[attr-defined]
        working = await s.get(WorkflowState, o.states["working"])
        assert working is not None
        working.checks_work = True
    await o.take(AGENT, task_id, w2)
    with pytest.raises(ConflictError) as exc:
        await o.move(AGENT, task_id, "closed", w2)
    assert exc.value.params == {"missing": "handoff"}


async def test_a_model_with_no_token_of_its_own_has_no_worker(_embedder: None) -> None:
    """A Telegram turn and a dispatched run act for the person with no token.
    Bound on "no token", they shared the person's workers, acted under them
    and closed them, and two runs shared each other's."""
    from mycelium_core.errors import NotFoundError

    o = await _org()
    task_id = await o.task()
    run = {"actor_kind": "agent_run", "actor_subject_id": str(uuid.uuid4())}
    for who in (TELEGRAM, run):
        with pytest.raises(ForbiddenError) as refused:
            await o.worker("mine", who)
        assert refused.value.code == MessageCode.WORKER_NEEDS_CREDENTIAL
    persons = await o.worker("person", PERSON)
    await o.move(PERSON, task_id, "working")
    for who in (TELEGRAM, run):
        with pytest.raises(NotFoundError):
            await o.take(who, task_id, persons)
        async with o.session(who) as s:  # type: ignore[attr-defined]
            with pytest.raises(NotFoundError):
                await workers_svc.close_worker(s, org_id=o.org, actor_id=o.user, worker_id=persons)
    await o.take(PERSON, task_id, persons)


async def test_another_member_s_worker_is_not_found(_embedder: None) -> None:
    """The credential is compared only once the person is: two people's
    sessions without a token look alike on every other column."""
    from mycelium_core.errors import NotFoundError
    from mycelium_core.services.memberships import add_member

    o = await _org()
    task_id = await o.task()
    email = f"{uuid.uuid4().hex[:10]}@example.test"
    async with admin_session() as s:
        await signup(s, email=email, password="pw-strong-123", org_name="throwaway")
    async with o.session(PERSON) as s:  # type: ignore[attr-defined]
        member = await add_member(s, org_id=o.org, actor_id=o.user, email=email, role="member")
    async with tenant_session(str(o.org), str(member), **PERSON) as s:  # type: ignore[arg-type]
        theirs = (
            await workers_svc.open_worker(s, org_id=o.org, actor_id=member, label="theirs")
        ).id
    await o.move(PERSON, task_id, "working")
    with pytest.raises(NotFoundError):
        await o.take(PERSON, task_id, theirs)


async def test_the_mcp_workflow_tools_carry_the_checking_station(_embedder: None) -> None:
    """``create_workflow`` and ``update_workflow`` over MCP pass the flag to
    the service, and an update that leaves it out keeps it. Writing a
    workflow is a person's act, so the path is stdio, with a person's
    session token: the HTTP gateway refuses both tools to an assistant."""
    from mycelium_mcp import server as mcp_server

    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCPWF",
        )
    assert r.token is not None
    token, org = r.token, str(r.org_id)
    states: list[dict[str, object]] = [
        {"name": "open", "ord": 0, "is_initial": True},
        {"name": "review", "ord": 1, "checks_work": True},
        {"name": "shut", "ord": 2, "is_terminal": True},
    ]
    transitions = [["open", "review"], ["review", "shut"]]

    async def flags(wf_id: str) -> dict[str, bool]:
        rows = await mcp_server.workflow_states(token=token, org_id=org, workflow_id=wf_id)
        return {st["name"]: st["checks_work"] for st in rows}

    made = await mcp_server.create_workflow(
        token=token, org_id=org, name="stations", states=states, transitions=transitions
    )
    wf_id = str(made["id"])
    assert await flags(wf_id) == {"open": False, "review": True, "shut": False}
    ids = {
        st["name"]: st["id"]
        for st in await mcp_server.workflow_states(token=token, org_id=org, workflow_id=wf_id)
    }
    edited = [
        {**{k: v for k, v in st.items() if k != "checks_work"}, "id": ids[str(st["name"])]}
        for st in states
    ]
    await mcp_server.update_workflow(
        token=token,
        org_id=org,
        workflow_id=wf_id,
        name="stations",
        states=edited,
        transitions=transitions,
    )
    assert await flags(wf_id) == {"open": False, "review": True, "shut": False}
    edited[0]["checks_work"] = True
    edited[1]["checks_work"] = False
    await mcp_server.update_workflow(
        token=token,
        org_id=org,
        workflow_id=wf_id,
        name="stations",
        states=edited,
        transitions=transitions,
    )
    assert await flags(wf_id) == {"open": True, "review": False, "shut": False}
