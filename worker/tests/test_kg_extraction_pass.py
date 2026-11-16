"""The garden loop's knowledge-graph pass (ADR-0071), against the real
Postgres and the real metering, with only the model faked.

- a pass reads the settled notes, writes proposed facts and records each
  read, and a second pass with nothing changed reads nothing;
- a note larger than the whole per-pass budget is recorded ``too_large``
  without a model call, and does not stop the notes behind it;
- a workspace out of credits ends the pass: nothing is marked failed, so
  the notes are read once credits are back instead of after an edit.

Seen failing on 2026-11-16: with ``read_autonomously`` no longer re-raising
the out-of-credits error, the last test fails (both notes marked failed).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterator, Sequence
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from mycelium_core.ai_providers import LLMResult, set_llm_override
from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.kg import KgEdge, KgNoteExtraction
from mycelium_core.models.note import NoteKind
from mycelium_core.models.note_part import NotePart
from mycelium_core.services import billing
from mycelium_core.services import notes as nt
from mycelium_core.services.auth import signup
from mycelium_worker import garden

_PAYLOAD = json.dumps(
    {
        "entities": [
            {"name": "Ann", "type": "person"},
            {"name": "Initech", "type": "organization"},
        ],
        "relations": [{"subject": "Ann", "predicate": "works_at", "object": "Initech"}],
    }
)


class _LLM:
    model_id = "fake-llm"
    calls = 0

    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        type(self).calls += 1
        return LLMResult(text=_PAYLOAD, tokens_in=50, tokens_out=50, model_id=self.model_id)


@pytest.fixture
def _llm() -> Iterator[type[_LLM]]:
    _LLM.calls = 0
    set_llm_override(_LLM)
    try:
        yield _LLM
    finally:
        set_llm_override(None)


async def _workspace(*, credits: int) -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s, email=f"{uuid.uuid4().hex[:10]}@example.test", password="pw-strong-123", org_name="W"
        )
    async with tenant_session(str(r.org_id), str(r.user_id)) as s:
        if credits:
            await billing.grant_credits(
                s, org_id=r.org_id, actor_id=r.user_id, amount=Decimal(credits)
            )
        await billing.upsert_rate_card(
            s,
            org_id=r.org_id,
            actor_id=r.user_id,
            model_id="fake-llm",
            provider="local",
            values={"credits_per_input": Decimal("0.01"), "credits_per_output": Decimal("0.01")},
        )
    return r.org_id, r.user_id


async def _settled_note(org: uuid.UUID, user: uuid.UUID, text: str) -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        n = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="n", text=text
        )
        await s.execute(
            update(NotePart)
            .where(NotePart.note_id == n.id)
            .values(updated_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1))
        )
        return n.id


async def _ledger(org: uuid.UUID, user: uuid.UUID) -> dict[uuid.UUID, str]:
    async with tenant_session(str(org), str(user)) as s:
        rows = (
            await s.execute(select(KgNoteExtraction).where(KgNoteExtraction.org_id == org))
        ).scalars()
        return {r.note_id: r.outcome for r in rows}


async def test_a_pass_reads_settled_notes_once_and_writes_proposals(_llm: type[_LLM]) -> None:
    org, user = await _workspace(credits=1000)
    a = await _settled_note(org, user, "Ann works at Initech.")
    b = await _settled_note(org, user, "Ann still works at Initech.")
    await garden._kg_extraction_pass(org, user)
    assert await _ledger(org, user) == {a: "ok", b: "ok"}
    assert _llm.calls == 2
    async with tenant_session(str(org), str(user)) as s:
        states = (
            await s.execute(select(KgEdge.review_state).where(KgEdge.org_id == org))
        ).scalars()
        assert set(states) == {"proposed"}
    await garden._kg_extraction_pass(org, user)
    assert _llm.calls == 2


async def test_a_note_over_the_whole_budget_is_set_aside_not_blocking(
    _llm: type[_LLM], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "kg_extract_tokens_per_pass", 2_000)
    org, user = await _workspace(credits=1000)
    huge = await _settled_note(org, user, "Ann works at Initech. " * 400)
    small = await _settled_note(org, user, "Ann works at Initech.")
    await garden._kg_extraction_pass(org, user)
    assert await _ledger(org, user) == {huge: "too_large", small: "ok"}
    assert _llm.calls == 1


async def test_running_out_of_credits_ends_the_pass_without_failing_notes(
    _llm: type[_LLM],
) -> None:
    org, user = await _workspace(credits=0)
    await _settled_note(org, user, "Ann works at Initech.")
    await _settled_note(org, user, "Ann still works at Initech.")
    await garden._kg_extraction_pass(org, user)
    assert await _ledger(org, user) == {}
    # Refused before the call: an unattended loop must not pay a provider
    # for a debit the wallet then refuses.
    assert _llm.calls == 0
