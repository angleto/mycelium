"""Conversational assistant (ADR-0026, P1) tests.

A scripted LLM drives the read-only ReAct loop over real seeded data:
list_tasks -> get_task -> finish, list_notes -> get_note -> finish, plain
final answer, and graceful recovery from a disallowed tool. Runs against
the real DB; the provider is injected (no model/network)."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Callable, Sequence

import pytest
from sqlalchemy import delete, select

from _fake_embedder import FakeEmbedder

from flow_core.ai_providers import LLMResult, set_llm_override
from flow_core.db import admin_session, tenant_session
from flow_core.embedder import set_embedder_override
from flow_core.models.note import Note, NoteKind
from flow_core.models.task import Task
from flow_core.models.telegram import TelegramAssistantJob, TelegramConversation
from flow_core.services import assistant as svc
from flow_core.services import notes as notes_svc
from flow_core.services import tasks as tasks_svc
from flow_core.services.auth import signup
from flow_core.telegram_client import (
    TelegramSendResult,
    TelegramSetWebhookResult,
    set_telegram_api_override,
)

_Step = str | Callable[[Sequence[tuple[str, str]]], str]


@pytest.fixture
async def _fake_embedder() -> AsyncIterator[None]:
    """Inject the ADR-0012 seam so the assistant's ``search`` tool
    exercises the real task-search pipeline against deterministic
    vectors -- the test asserts on the actual hit list, not on a
    mocked counter."""
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


@pytest.fixture(autouse=True)
async def _clear_assistant_queue() -> AsyncIterator[None]:
    """The assistant job queue + conversation state are global (one bot,
    accessed from admin_session). Isolate tests by clearing both before
    each so a leftover pending job from one test does not get drained by
    another's ``process_pending_jobs``."""
    async with admin_session() as s:
        await s.execute(delete(TelegramAssistantJob))
        await s.execute(delete(TelegramConversation))
    yield


class _ScriptLLM:
    """Deterministic provider: emits a scripted decision per step. A step
    is either a fixed string or a callable of the conversation so far
    (so a later step can read an id out of the prior observation)."""

    model_id = "fake-llm"

    def __init__(self, steps: list[_Step]) -> None:
        self._steps = steps
        self._i = 0

    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        step = self._steps[min(self._i, len(self._steps) - 1)]
        self._i += 1
        text = step(messages) if callable(step) else step
        return LLMResult(text=text, tokens_in=1, tokens_out=1, model_id=self.model_id)


def _first_id(obs: str) -> str:
    m = re.search(r"id=([0-9a-fA-F-]{36})", obs)
    return m.group(1) if m else ""


def _email() -> str:
    return f"asst-{uuid.uuid4().hex[:12]}@example.com"


async def _signup() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="ASST")
    return r.org_id, r.user_id


async def test_plain_answer_passthrough() -> None:
    org, user = await _signup()
    llm = _ScriptLLM(['{"tool": "finish", "args": {"output": "Hello, I can help."}}'])
    reply = await svc.run_turn(
        org_id=org, user_id=user, text="hi", turn_key=uuid.uuid4().hex, provider=llm
    )
    assert reply == "Hello, I can help."


async def test_non_json_answer_is_treated_as_final() -> None:
    org, user = await _signup()
    # A model that "just answered" in prose still terminates safely.
    llm = _ScriptLLM(["You have nothing urgent today."])
    reply = await svc.run_turn(
        org_id=org, user_id=user, text="what's up", turn_key=uuid.uuid4().hex, provider=llm
    )
    assert reply == "You have nothing urgent today."


async def test_list_then_get_task_reads_real_data() -> None:
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="Pay the invoice")
        task_title = task.title
    steps: list[_Step] = [
        '{"tool": "list_tasks", "args": {}}',
        lambda msgs: json.dumps({"tool": "get_task", "args": {"id": _first_id(msgs[-1][1])}}),
        lambda msgs: json.dumps({"tool": "finish", "args": {"output": msgs[-1][1]}}),
    ]
    reply = await svc.run_turn(
        org_id=org,
        user_id=user,
        text="show my tasks",
        turn_key=uuid.uuid4().hex,
        provider=_ScriptLLM(steps),
    )
    # The finish echoes the get_task observation, which carries the title.
    assert task_title in reply


async def test_list_then_get_note_reads_real_data() -> None:
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        note = await notes_svc.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title=None, text="remember the milk"
        )
        _ = note.id
    steps: list[_Step] = [
        '{"tool": "list_notes", "args": {}}',
        lambda msgs: json.dumps({"tool": "get_note", "args": {"id": _first_id(msgs[-1][1])}}),
        lambda msgs: json.dumps({"tool": "finish", "args": {"output": msgs[-1][1]}}),
    ]
    reply = await svc.run_turn(
        org_id=org,
        user_id=user,
        text="read my latest note",
        turn_key=uuid.uuid4().hex,
        provider=_ScriptLLM(steps),
    )
    assert "remember the milk" in reply


async def test_disallowed_tool_yields_error_observation_then_recovers() -> None:
    org, user = await _signup()
    steps: list[_Step] = [
        '{"tool": "delete_everything", "args": {}}',
        lambda msgs: json.dumps({"tool": "finish", "args": {"output": msgs[-1][1]}}),
    ]
    reply = await svc.run_turn(
        org_id=org,
        user_id=user,
        text="nuke it",
        turn_key=uuid.uuid4().hex,
        provider=_ScriptLLM(steps),
    )
    # The write/unknown tool never executes; the loop observes an error and
    # the model can recover. No exception escapes.
    assert "not available" in reply.lower()


async def test_step_cap_returns_graceful_message() -> None:
    org, user = await _signup()
    # A model that never finishes (always lists) must stop at the cap.
    llm = _ScriptLLM(['{"tool": "list_tasks", "args": {}}'])
    reply = await svc.run_turn(
        org_id=org, user_id=user, text="loop", turn_key=uuid.uuid4().hex, provider=llm
    )
    assert reply  # non-empty graceful fallback, no hang/exception


# --- P2: scoped writes -----------------------------------------------------


def _created_task_id(msgs: Sequence[tuple[str, str]]) -> str:
    for _, content in msgs:
        m = re.search(r"created task id=([0-9a-fA-F-]{36})", content)
        if m:
            return m.group(1)
    return ""


def _first_transition_name(obs: str) -> str:
    m = re.search(r": (.+?) \(id=", obs)
    return m.group(1).strip() if m else ""


async def test_create_note_via_tool_persists() -> None:
    org, user = await _signup()
    steps: list[_Step] = [
        '{"tool": "create_note", "args": {"text": "buy milk", "title": "Shopping"}}',
        lambda msgs: json.dumps({"tool": "finish", "args": {"output": msgs[-1][1]}}),
    ]
    reply = await svc.run_turn(
        org_id=org,
        user_id=user,
        text="note that I should buy milk",
        turn_key=uuid.uuid4().hex,
        provider=_ScriptLLM(steps),
    )
    assert "created note" in reply.lower()
    async with tenant_session(str(org), str(user)) as s:
        rows = (await s.execute(select(Note).where(Note.transcript == "buy milk"))).scalars().all()
        assert len(rows) == 1


async def test_create_task_then_set_state_transitions() -> None:
    org, user = await _signup()
    steps: list[_Step] = [
        '{"tool": "create_task", "args": {"title": "Ship the release"}}',
        lambda msgs: json.dumps(
            {"tool": "list_task_transitions", "args": {"id": _created_task_id(msgs)}}
        ),
        lambda msgs: json.dumps(
            {
                "tool": "set_task_state",
                "args": {
                    "id": _created_task_id(msgs),
                    "state": _first_transition_name(msgs[-1][1]),
                },
            }
        ),
        lambda msgs: json.dumps({"tool": "finish", "args": {"output": msgs[-1][1]}}),
    ]
    reply = await svc.run_turn(
        org_id=org,
        user_id=user,
        text="create a task and start it",
        turn_key=uuid.uuid4().hex,
        provider=_ScriptLLM(steps),
    )
    # The set_task_state observation is "task <id> -> <Name>"; the default
    # workflow's initial state has at least one outgoing transition.
    assert "->" in reply and "error" not in reply.lower()


async def test_update_task_priority_persists() -> None:
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="reprioritize me", importance=3, urgency=3
        )
        tid = task.id
    steps: list[_Step] = [
        json.dumps({"tool": "update_task", "args": {"id": str(tid), "priority": 1}}),
        lambda msgs: json.dumps({"tool": "finish", "args": {"output": msgs[-1][1]}}),
    ]
    reply = await svc.run_turn(
        org_id=org,
        user_id=user,
        text="make it top priority",
        turn_key=uuid.uuid4().hex,
        provider=_ScriptLLM(steps),
    )
    assert "updated task" in reply.lower()
    async with tenant_session(str(org), str(user)) as s:
        t = (await s.execute(select(Task).where(Task.id == tid))).scalar_one()
        assert t.priority == 1


async def test_search_tool_returns_matching_task(_fake_embedder: None) -> None:
    """Task a83a5c0b: the assistant can call ``search`` and gets back a
    compact ``- task:<uuid> | <title> -- <snippet>`` line per hit. The
    surrounding ReAct loop then has a navigable id it can pass to
    ``get_task`` for detail."""
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="Quarterly budget for project alpha-xkz",
        )
        tid = task.id
    steps: list[_Step] = [
        json.dumps({"tool": "search", "args": {"q": "alpha-xkz", "limit": 5}}),
        lambda msgs: json.dumps({"tool": "finish", "args": {"output": msgs[-1][1]}}),
    ]
    reply = await svc.run_turn(
        org_id=org,
        user_id=user,
        text="find the budget task",
        turn_key=uuid.uuid4().hex,
        provider=_ScriptLLM(steps),
    )
    assert "search:" in reply
    assert str(tid) in reply, f"expected task id {tid} in search output, got: {reply}"


async def test_search_tool_empty_query_returns_error_observation(
    _fake_embedder: None,
) -> None:
    """A missing/empty ``q`` is reported as an error observation -- the
    surrounding loop can recover (the next decision sees the error
    string and picks a different tool / finishes gracefully)."""
    org, user = await _signup()
    steps: list[_Step] = [
        json.dumps({"tool": "search", "args": {"q": ""}}),
        lambda msgs: json.dumps({"tool": "finish", "args": {"output": msgs[-1][1]}}),
    ]
    reply = await svc.run_turn(
        org_id=org,
        user_id=user,
        text="search nothing",
        turn_key=uuid.uuid4().hex,
        provider=_ScriptLLM(steps),
    )
    assert "error" in reply.lower() and "args.q" in reply


# --- P3: durable queue + worker + multi-turn --------------------------------


class _CountingLLM:
    """Finishes immediately with the number of messages it was given, so a
    test can prove prior conversation turns were loaded into context."""

    model_id = "fake-llm"

    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        out = json.dumps({"tool": "finish", "args": {"output": str(len(messages))}})
        return LLMResult(text=out, tokens_in=1, tokens_out=1, model_id=self.model_id)


class _FakeTg:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, *, chat_id: int, text: str) -> TelegramSendResult:
        self.sent.append((chat_id, text))
        return TelegramSendResult(message_id=len(self.sent))

    async def set_webhook(self, *, url: str, secret_token: str) -> TelegramSetWebhookResult:
        return TelegramSetWebhookResult(ok=True, description="ok")

    async def get_file_path(self, *, file_id: str) -> str:
        return "x"

    async def download_file(self, *, file_path: str) -> bytes:
        return b""


def _chat_id() -> int:
    return uuid.uuid4().int & 0x7FFFFFFF


async def test_enqueue_then_worker_processes_and_replies() -> None:
    org, user = await _signup()
    chat_id = _chat_id()
    async with admin_session() as s:
        await svc.enqueue_turn(
            s, org_id=org, user_id=user, chat_id=chat_id, update_id=_chat_id(), text="hi"
        )
    fake = _FakeTg()
    set_llm_override(_CountingLLM)
    set_telegram_api_override(lambda: fake)
    try:
        processed = await svc.process_pending_jobs(limit=10)
    finally:
        set_llm_override(None)
        set_telegram_api_override(None)
    assert processed == 1
    # The reply was sent to the right chat.
    assert any(cid == chat_id for cid, _ in fake.sent)
    async with admin_session() as s:
        jobs = (
            (
                await s.execute(
                    select(TelegramAssistantJob).where(TelegramAssistantJob.chat_id == chat_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(jobs) == 1 and jobs[0].status == "done"
        conv = (
            await s.execute(
                select(TelegramConversation).where(TelegramConversation.chat_id == chat_id)
            )
        ).scalar_one()
        assert len(conv.turns) == 2  # user + assistant


async def test_second_turn_sees_prior_history() -> None:
    org, user = await _signup()
    chat_id = _chat_id()
    fake = _FakeTg()
    set_llm_override(_CountingLLM)
    set_telegram_api_override(lambda: fake)
    try:
        async with admin_session() as s:
            await svc.enqueue_turn(
                s, org_id=org, user_id=user, chat_id=chat_id, update_id=_chat_id(), text="first"
            )
        await svc.process_pending_jobs(limit=10)
        async with admin_session() as s:
            await svc.enqueue_turn(
                s, org_id=org, user_id=user, chat_id=chat_id, update_id=_chat_id(), text="second"
            )
        await svc.process_pending_jobs(limit=10)
    finally:
        set_llm_override(None)
        set_telegram_api_override(None)
    # _CountingLLM replies with len(messages). First turn: [user] -> "1".
    # Second turn: [prior user, prior assistant, new user] -> "3".
    replies = [text for _, text in fake.sent]
    assert replies == ["1", "3"]


async def test_enqueue_is_idempotent_by_update_id() -> None:
    org, user = await _signup()
    chat_id = _chat_id()
    uid = _chat_id()
    async with admin_session() as s:
        await svc.enqueue_turn(
            s, org_id=org, user_id=user, chat_id=chat_id, update_id=uid, text="a"
        )
        await svc.enqueue_turn(
            s, org_id=org, user_id=user, chat_id=chat_id, update_id=uid, text="a again"
        )
    async with admin_session() as s:
        jobs = (
            (
                await s.execute(
                    select(TelegramAssistantJob).where(TelegramAssistantJob.update_id == uid)
                )
            )
            .scalars()
            .all()
        )
        assert len(jobs) == 1
