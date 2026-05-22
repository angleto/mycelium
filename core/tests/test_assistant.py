"""Conversational assistant (ADR-0026, P1) tests.

A scripted LLM drives the read-only ReAct loop over real seeded data:
list_tasks -> get_task -> finish, list_notes -> get_note -> finish, plain
final answer, and graceful recovery from a disallowed tool. Runs against
the real DB; the provider is injected (no model/network)."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Sequence

from flow_core.ai_providers import LLMResult
from flow_core.db import admin_session, tenant_session
from flow_core.models.note import NoteKind
from flow_core.services import assistant as svc
from flow_core.services import notes as notes_svc
from flow_core.services import tasks as tasks_svc
from flow_core.services.auth import signup

_Step = str | Callable[[Sequence[tuple[str, str]]], str]


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
