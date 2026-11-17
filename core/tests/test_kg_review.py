"""The knowledge graph's review gate and its autonomous source (ADR-0071).

Asserts, against the real Postgres:

- an autonomous read is born ``proposed`` whatever the configuration, and a
  person's decision is the only way it becomes effective;
- approve and reject act on proposals only, guard the version the reviewer
  read, are idempotent, and a rejection is never read as a past belief;
- a rejected triple is not proposed again by the pass, and is by a person's
  ``kg_extract``, whose facts are proposals too;
- the pass reads only what a person could have written and wants seen, in
  its current form, once, and within its token budget.

Seen failing on 2026-11-16 (base 4905ef7c plus this change), each defect put
back alone into ``services/kg.py`` and only its test run:

- autonomous facts following the default-off ``garden_review_gate_enabled``
  -> test_autonomous_read_is_proposed_and_withheld_until_approved;
- ``reject_fact`` without the proposal check (the old delegation to
  ``invalidate_fact``) -> test_reject_acts_on_proposals_only;
- the effective predicate excluding only 'proposed'
  -> test_reject_is_idempotent_final_and_never_a_past_belief;
- no rejected-triple check -> test_a_rejected_triple_is_not_proposed_again;
- a signature over part id, version and order instead of text
  -> test_the_pass_reads_once_and_again_only_after_an_edit (and, before it
  was fixed, test_a_rejected_triple_is_not_proposed_again);
- no ``index_scope`` filter -> test_the_pass_skips_what_a_person_kept_out.

Seen failing on 2026-11-17, same method: ``kg_extract`` writing effective
facts -> both test_a_persons_extraction_* tests; ``kg_extract`` creating
the entities it only declared -> test_a_persons_extraction_is_proposed_too;
the rejected-triple check applied to ``kg_extract`` too
-> test_a_persons_extraction_proposes_a_rejected_triple_again.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import uuid
from collections.abc import Iterator, Sequence
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select, update  # noqa: E402

from mycelium_core.ai_providers import LLMResult, set_llm_override  # noqa: E402
from mycelium_core.db import admin_session, tenant_session  # noqa: E402
from mycelium_core.errors import ConflictError, DomainError, NotFoundError  # noqa: E402
from mycelium_core.i18n import MessageCode  # noqa: E402
from mycelium_core.models.billing import UsageRecord  # noqa: E402
from mycelium_core.models.index_scope import IndexScope  # noqa: E402
from mycelium_core.models.kg import KgEdge, KgEntity, KgNoteExtraction  # noqa: E402
from mycelium_core.models.note import Note, NoteKind  # noqa: E402
from mycelium_core.models.note_part import NotePart  # noqa: E402
from mycelium_core.services import billing, kg  # noqa: E402
from mycelium_core.services import notes as nt  # noqa: E402
from mycelium_core.services.auth import signup  # noqa: E402

_PAYLOAD = json.dumps(
    {
        "entities": [
            {"name": "Bob", "type": "person"},
            {"name": "Globex", "type": "organization"},
        ],
        "relations": [{"subject": "Bob", "predicate": "works at", "object": "Globex"}],
    }
)


class _ExtractLLM:
    model_id = "fake-llm"

    def __init__(self, payload: str = _PAYLOAD) -> None:
        self._payload = payload
        self.calls = 0

    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        self.calls += 1
        return LLMResult(text=self._payload, tokens_in=5, tokens_out=5, model_id=self.model_id)


class _BrokenLLM:
    model_id = "fake-llm"

    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        raise RuntimeError("provider down")


async def _org(*, credits: int = 1000) -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s, email=f"{uuid.uuid4().hex[:10]}@example.test", password="pw-strong-123", org_name="K"
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
            values={"credits_per_input": Decimal("0.001"), "credits_per_output": Decimal("0.001")},
        )
    return r.org_id, r.user_id


async def _note(org: uuid.UUID, user: uuid.UUID, text: str = "Bob works at Globex.") -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        n = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="n", text=text
        )
        return n.id


async def _edit(org: uuid.UUID, user: uuid.UUID, note_id: uuid.UUID, text: str) -> None:
    async with tenant_session(str(org), str(user)) as s:
        version = (await s.execute(select(Note.version).where(Note.id == note_id))).scalar_one()
        await nt.update_note(
            s, org_id=org, actor_id=user, note_id=note_id, expected_version=version, text=text
        )


async def _settle(org: uuid.UUID, user: uuid.UUID, note_id: uuid.UUID) -> None:
    """Move the note's last edit into the past, as if left alone for a day."""
    async with tenant_session(str(org), str(user)) as s:
        await s.execute(
            update(NotePart)
            .where(NotePart.note_id == note_id)
            .values(updated_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1))
        )


async def _pending(
    org: uuid.UUID, user: uuid.UUID, *, token_budget: int = 20_000
) -> list[kg.PendingRead]:
    async with tenant_session(str(org), str(user)) as s:
        return await kg.pending_reads(
            s,
            org_id=org,
            settled_before=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=15),
            max_attempts=3,
            token_budget=token_budget,
            scan=100,
        )


async def _read_all(org: uuid.UUID, user: uuid.UUID, llm: object) -> list[str | None]:
    out: list[str | None] = []
    for p in await _pending(org, user):
        async with tenant_session(str(org), str(user)) as s:
            out.append(
                await kg.read_autonomously(s, org_id=org, actor_id=user, pending=p, llm=llm)  # type: ignore[arg-type]
            )
    return out


async def _proposed_edge(org: uuid.UUID, user: uuid.UUID) -> KgEdge:
    async with tenant_session(str(org), str(user)) as s:
        x = await kg.ensure_entity(s, org_id=org, name="X", entity_type="person")
        y = await kg.ensure_entity(s, org_id=org, name="Y", entity_type="organization")
        return await kg.add_fact(
            s,
            org_id=org,
            subject_id=x.id,
            predicate="member_of",
            object_id=y.id,
            review_state="proposed",
        )


# ── the review gate ────────────────────────────────────────────────────────


async def test_autonomous_read_is_proposed_and_withheld_until_approved() -> None:
    org, user = await _org()
    nid = await _note(org, user)
    await _settle(org, user, nid)
    assert await _read_all(org, user, _ExtractLLM()) == ["ok"]
    async with tenant_session(str(org), str(user)) as s:
        edge = (await s.execute(select(KgEdge).where(KgEdge.org_id == org))).scalar_one()
        assert edge.review_state == "proposed"
        assert await kg.entity_facts(s, org_id=org, actor_id=user, entity_id=edge.subject_id) == []
        listed = await kg.list_proposed_facts(s, org_id=org, actor_id=user, limit=10)
        assert [f.edge_id for f in listed] == [edge.id]
        assert listed[0].source_note_id == nid
        assert listed[0].origin_model_id == "fake-llm"
        await kg.approve_fact(
            s, org_id=org, actor_id=user, edge_id=edge.id, expected_version=listed[0].version
        )
        facts = await kg.entity_facts(s, org_id=org, actor_id=user, entity_id=edge.subject_id)
        assert [f.edge_id for f in facts] == [edge.id]
        assert await kg.list_proposed_facts(s, org_id=org, actor_id=user, limit=10) == []


async def test_approve_and_reject_guard_the_version_read() -> None:
    org, user = await _org()
    edge = await _proposed_edge(org, user)
    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(ConflictError):
            await kg.approve_fact(
                s, org_id=org, actor_id=user, edge_id=edge.id, expected_version=edge.version + 1
            )
        with pytest.raises(ConflictError):
            await kg.reject_fact(
                s, org_id=org, actor_id=user, edge_id=edge.id, expected_version=edge.version + 1
            )


async def test_reject_acts_on_proposals_only() -> None:
    """``reject_fact`` used to invalidate whatever it was given, so
    "reject" could tombstone a fact a person had asserted. A fact that never
    went through review is not a proposal and is absent here."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await kg.ensure_entity(s, org_id=org, name="A", entity_type="person")
        b = await kg.ensure_entity(s, org_id=org, name="B", entity_type="person")
        asserted = await kg.add_fact(
            s, org_id=org, subject_id=a.id, predicate="knows", object_id=b.id
        )
        with pytest.raises(NotFoundError):
            await kg.reject_fact(s, org_id=org, actor_id=user, edge_id=asserted.id)
        with pytest.raises(NotFoundError):
            await kg.approve_fact(s, org_id=org, actor_id=user, edge_id=asserted.id)
        facts = await kg.entity_facts(s, org_id=org, actor_id=user, entity_id=a.id)
        assert [f.edge_id for f in facts] == [asserted.id]


async def test_reject_is_idempotent_final_and_never_a_past_belief() -> None:
    org, user = await _org()
    edge = await _proposed_edge(org, user)
    before = dt.datetime.now(dt.UTC)
    async with tenant_session(str(org), str(user)) as s:
        rejected = await kg.reject_fact(s, org_id=org, actor_id=user, edge_id=edge.id)
        assert rejected.review_state == "rejected"
        assert rejected.invalidated_at is not None
        again = await kg.reject_fact(s, org_id=org, actor_id=user, edge_id=edge.id)
        assert again.version == rejected.version
        with pytest.raises(NotFoundError):
            await kg.approve_fact(s, org_id=org, actor_id=user, edge_id=edge.id)
        # The transaction-time read before the rejection: the fact was a
        # proposal then, never a belief, so it must not appear.
        believed = await kg.entity_facts(
            s, org_id=org, actor_id=user, entity_id=edge.subject_id, tx_as_of=before
        )
        assert believed == []


async def test_a_rejected_triple_is_not_proposed_again() -> None:
    org, user = await _org()
    nid = await _note(org, user)
    await _settle(org, user, nid)
    await _read_all(org, user, _ExtractLLM())
    async with tenant_session(str(org), str(user)) as s:
        edge = (await s.execute(select(KgEdge).where(KgEdge.org_id == org))).scalar_one()
        await kg.reject_fact(s, org_id=org, actor_id=user, edge_id=edge.id)
    # The note changes, so the pass reads it again; the model says the same.
    await _edit(org, user, nid, "Bob works at Globex!")
    await _settle(org, user, nid)
    assert await _read_all(org, user, _ExtractLLM()) == ["ok"]
    async with tenant_session(str(org), str(user)) as s:
        assert await kg.list_proposed_facts(s, org_id=org, actor_id=user, limit=10) == []


async def test_a_persons_extraction_is_proposed_too() -> None:
    """Decided 2026-11-17: asking for an extraction is not vouching for what
    it finds, so ``kg_extract`` writes the same proposals the pass does, and
    an entity the model named without relating it is not created."""
    org, user = await _org()
    nid = await _note(org, user)
    payload = json.dumps(
        {
            "entities": [
                {"name": "Bob", "type": "person"},
                {"name": "Globex", "type": "organization"},
                {"name": "Solo", "type": "person"},
            ],
            "relations": [{"subject": "Bob", "predicate": "works at", "object": "Globex"}],
        }
    )
    async with tenant_session(str(org), str(user)) as s:
        res = await kg.extract_facts(
            s, org_id=org, actor_id=user, note_id=nid, llm=_ExtractLLM(payload)
        )
        edge = (await s.execute(select(KgEdge).where(KgEdge.id == res.edge_ids[0]))).scalar_one()
        assert edge.review_state == "proposed"
        assert await kg.entity_facts(s, org_id=org, actor_id=user, entity_id=edge.subject_id) == []
        assert await kg.search_entities(s, org_id=org, actor_id=user, query="") == []
        names = (await s.execute(select(KgEntity.name).where(KgEntity.org_id == org))).scalars()
        assert set(names) == {"Bob", "Globex"}
        listed = await kg.list_proposed_facts(s, org_id=org, actor_id=user, limit=10)
        assert [f.edge_id for f in listed] == [edge.id]


async def test_a_persons_extraction_proposes_a_rejected_triple_again() -> None:
    """The pass leaves a rejected triple alone; a person running
    ``kg_extract`` is asking anew, and it is the only way back for a triple
    rejected by mistake."""
    org, user = await _org()
    nid = await _note(org, user)
    async with tenant_session(str(org), str(user)) as s:
        first = await kg.extract_facts(s, org_id=org, actor_id=user, note_id=nid, llm=_ExtractLLM())
        await kg.reject_fact(s, org_id=org, actor_id=user, edge_id=first.edge_ids[0])
        again = await kg.extract_facts(s, org_id=org, actor_id=user, note_id=nid, llm=_ExtractLLM())
        assert len(again.edge_ids) == 1 and again.edge_ids != first.edge_ids
        listed = await kg.list_proposed_facts(s, org_id=org, actor_id=user, limit=10)
        assert [f.edge_id for f in listed] == again.edge_ids


async def test_proposed_listing_pages_by_cursor_without_overlap() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        hub = await kg.ensure_entity(s, org_id=org, name="Hub", entity_type="organization")
        for i in range(5):
            e = await kg.ensure_entity(s, org_id=org, name=f"P{i}", entity_type="person")
            await kg.add_fact(
                s,
                org_id=org,
                subject_id=e.id,
                predicate="member_of",
                object_id=hub.id,
                review_state="proposed",
            )
    async with tenant_session(str(org), str(user)) as s:
        first = await kg.list_proposed_facts(s, org_id=org, actor_id=user, limit=3)
        last = first[-1]
        rest = await kg.list_proposed_facts(
            s, org_id=org, actor_id=user, limit=10, after=(last.created_at, last.edge_id)
        )
    ids = [f.edge_id for f in first] + [f.edge_id for f in rest]
    assert len(ids) == 5 == len(set(ids))


# ── what the pass reads ────────────────────────────────────────────────────


async def test_the_pass_reads_once_and_again_only_after_an_edit() -> None:
    org, user = await _org()
    nid = await _note(org, user)
    await _settle(org, user, nid)
    llm = _ExtractLLM()
    assert await _read_all(org, user, llm) == ["ok"]
    assert await _read_all(org, user, llm) == []
    assert llm.calls == 1
    await _edit(org, user, nid, "Bob left Globex.")
    # Just edited: not settled yet, so not read.
    assert await _pending(org, user) == []
    await _settle(org, user, nid)
    assert await _read_all(org, user, llm) == ["ok"]
    assert llm.calls == 2


async def test_the_pass_skips_what_a_person_kept_out() -> None:
    """Opted out of automatic indexing, archived, trashed, still a proposal:
    none of these is read by a model nobody asked."""
    org, user = await _org()
    opted_out, archived, trashed, proposal, ordinary = [await _note(org, user) for _ in range(5)]
    async with tenant_session(str(org), str(user)) as s:
        await s.execute(
            update(Note).where(Note.id == opted_out).values(index_scope=IndexScope.none)
        )
        await s.execute(update(Note).where(Note.id == archived).values(is_archived=True))
        await s.execute(
            update(Note).where(Note.id == trashed).values(deleted_at=dt.datetime.now(dt.UTC))
        )
        await s.execute(update(Note).where(Note.id == proposal).values(review_state="proposed"))
    for nid in (opted_out, archived, trashed, proposal, ordinary):
        await _settle(org, user, nid)
    assert [p.note_id for p in await _pending(org, user)] == [ordinary]


async def test_a_failed_read_is_retried_up_to_the_limit_then_left() -> None:
    org, user = await _org()
    nid = await _note(org, user)
    await _settle(org, user, nid)
    for _ in range(3):
        assert await _read_all(org, user, _BrokenLLM()) == ["failed"]
    assert await _pending(org, user) == []
    async with tenant_session(str(org), str(user)) as s:
        row = (
            await s.execute(select(KgNoteExtraction).where(KgNoteExtraction.note_id == nid))
        ).scalar_one()
        assert (row.outcome, row.attempts) == ("failed", 3)
        # Nothing the failed reads touched survived their savepoint.
        assert (await s.execute(select(KgEdge).where(KgEdge.org_id == org))).first() is None


def _pending_read(tokens: int) -> kg.PendingRead:
    return kg.PendingRead(
        note_id=uuid.uuid4(), parts_signature="s", estimated_tokens=tokens, attempts=0
    )


def test_the_plan_charges_each_note_its_own_estimate_and_never_skips_ahead() -> None:
    a, b, c, d = (_pending_read(t) for t in (400, 500, 300, 100))
    plan = kg.plan_pass([a, b, c, d], token_budget=1000)
    # a + b = 900; c (300) does not fit the 100 left, and d behind it waits
    # too, so c is first in line next time instead of being overtaken forever.
    assert plan.read == [a, b]
    assert plan.too_large == []


def test_a_note_larger_than_the_whole_budget_is_set_aside_not_blocking() -> None:
    huge, small = _pending_read(5000), _pending_read(200)
    plan = kg.plan_pass([huge, small], token_budget=1000)
    assert plan.too_large == [huge]
    assert plan.read == [small]


def test_the_estimate_grows_with_the_note() -> None:
    assert kg.estimate_read_tokens(10_000) > kg.estimate_read_tokens(100) > 0


# ── found by the adversarial review of 2026-11-16 ─────────────────────────
#
# Each seen failing against the defect it names, the defect put back alone:
# the single savepoint around call and writes (paid_read test), no wallet
# check before the call (empty_wallet test), _parse_extraction's empty
# answer recorded ok (unreadable test), too_large never re-listed
# (budget test), add_fact raising on one inverted window (inverted test),
# no per-note lock (lock test), search_entities listing every entity
# (hidden test), retracted facts not remembered (retracted test).


class _MeteredOverride:
    """Through resolve_llm, so the real MeteredLLM meters it."""

    model_id = "fake-llm"
    calls = 0
    payload = _PAYLOAD

    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        type(self).calls += 1
        return LLMResult(text=type(self).payload, tokens_in=50, tokens_out=50, model_id="fake-llm")


@pytest.fixture
def _metered() -> Iterator[type[_MeteredOverride]]:
    _MeteredOverride.calls = 0
    _MeteredOverride.payload = _PAYLOAD
    set_llm_override(_MeteredOverride)
    try:
        yield _MeteredOverride
    finally:
        set_llm_override(None)


async def _usage(org: uuid.UUID, user: uuid.UUID) -> int:
    async with tenant_session(str(org), str(user)) as s:
        rows = (await s.execute(select(UsageRecord).where(UsageRecord.org_id == org))).scalars()
        return len([r for r in rows if r.operation_id.startswith("kg_extract:")])


async def test_a_paid_read_whose_writes_fail_keeps_its_charge(
    _metered: type[_MeteredOverride], monkeypatch: pytest.MonkeyPatch
) -> None:
    org, user = await _org()
    nid = await _note(org, user)
    await _settle(org, user, nid)

    async def _boom(*a: object, **k: object) -> None:
        raise RuntimeError("write failed")

    monkeypatch.setattr(kg, "_write_reading", _boom)
    assert await _read_all(org, user, None) == ["failed"]
    assert _metered.calls == 1
    assert await _usage(org, user) == 1
    # The retry is a new call, so it is a new charge, not a replay.
    assert await _read_all(org, user, None) == ["failed"]
    assert await _usage(org, user) == 2


async def test_an_empty_wallet_refuses_before_the_provider_is_called(
    _metered: type[_MeteredOverride],
) -> None:
    org, user = await _org(credits=0)
    nid = await _note(org, user)
    await _settle(org, user, nid)
    (pending,) = await _pending(org, user)
    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(DomainError) as exc:
            await kg.read_autonomously(s, org_id=org, actor_id=user, pending=pending)
    assert exc.value.code == MessageCode.INSUFFICIENT_CREDITS
    assert _metered.calls == 0


async def test_an_unreadable_answer_is_a_failed_read_not_an_empty_one(
    _metered: type[_MeteredOverride],
) -> None:
    org, user = await _org()
    nid = await _note(org, user)
    await _settle(org, user, nid)
    _metered.payload = "Sure! Here are the facts: Bob works at"
    assert await _read_all(org, user, None) == ["failed"]
    assert await _usage(org, user) == 1
    assert [p.note_id for p in await _pending(org, user)] == [nid]


async def test_a_too_large_note_returns_once_the_budget_can_hold_it() -> None:
    org, user = await _org()
    nid = await _note(org, user, "Bob works at Globex. " * 200)
    await _settle(org, user, nid)
    (p,) = await _pending(org, user, token_budget=1_000)
    assert p.estimated_tokens > 1_000
    async with tenant_session(str(org), str(user)) as s:
        await kg.record_too_large(s, org_id=org, pending=p)
    assert await _pending(org, user, token_budget=1_000) == []
    assert [x.note_id for x in await _pending(org, user, token_budget=p.estimated_tokens)] == [nid]


async def test_one_inverted_window_does_not_take_down_the_other_facts() -> None:
    org, user = await _org()
    nid = await _note(org, user)
    await _settle(org, user, nid)
    payload = json.dumps(
        {
            "entities": [],
            "relations": [
                {"subject": "Bob", "predicate": "works_at", "object": "Globex"},
                {
                    "subject": "Bob",
                    "predicate": "lives_in",
                    "object": "Rome",
                    "valid_from": "2024-01-01",
                    "valid_to": "2020-01-01",
                },
            ],
        }
    )
    assert await _read_all(org, user, _ExtractLLM(payload)) == ["ok"]
    async with tenant_session(str(org), str(user)) as s:
        listed = await kg.list_proposed_facts(s, org_id=org, actor_id=user, limit=10)
    assert [f.predicate for f in listed] == ["works_at"]


async def test_a_read_another_pass_holds_is_skipped_without_a_call() -> None:
    org, user = await _org()
    nid = await _note(org, user)
    await _settle(org, user, nid)
    (p,) = await _pending(org, user)
    llm = _ExtractLLM()
    async with tenant_session(str(org), str(user)) as holder:
        assert not await kg._taken_elsewhere(holder, org_id=org, pending=p)
        async with tenant_session(str(org), str(user)) as s:
            assert (
                await kg.read_autonomously(s, org_id=org, actor_id=user, pending=p, llm=llm)  # type: ignore[arg-type]
                is None
            )
    assert llm.calls == 0


async def test_entities_of_unapproved_facts_are_hidden_from_search() -> None:
    org, user = await _org()
    nid = await _note(org, user)
    await _settle(org, user, nid)
    await _read_all(org, user, _ExtractLLM())
    async with tenant_session(str(org), str(user)) as s:
        assert await kg.search_entities(s, org_id=org, actor_id=user, query="bob") == []
        (fact,) = await kg.list_proposed_facts(s, org_id=org, actor_id=user, limit=10)
        await kg.approve_fact(s, org_id=org, actor_id=user, edge_id=fact.edge_id)
        found = await kg.search_entities(s, org_id=org, actor_id=user, query="bob")
        assert [e.name for e in found] == ["Bob"]
        assert found[0].entity_type == "person"


async def test_a_retracted_fact_is_not_proposed_again() -> None:
    org, user = await _org()
    nid = await _note(org, user)
    await _settle(org, user, nid)
    await _read_all(org, user, _ExtractLLM())
    async with tenant_session(str(org), str(user)) as s:
        (fact,) = await kg.list_proposed_facts(s, org_id=org, actor_id=user, limit=10)
        await kg.approve_fact(s, org_id=org, actor_id=user, edge_id=fact.edge_id)
        await kg.invalidate_fact(s, org_id=org, actor_id=user, edge_id=fact.edge_id)
    await _edit(org, user, nid, "Bob works at Globex, still.")
    await _settle(org, user, nid)
    assert await _read_all(org, user, _ExtractLLM()) == ["ok"]
    async with tenant_session(str(org), str(user)) as s:
        assert await kg.list_proposed_facts(s, org_id=org, actor_id=user, limit=10) == []


async def test_a_note_of_blank_space_is_not_read() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        n = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="blank", text="  \n\t "
        )
    await _settle(org, user, n.id)
    assert await _pending(org, user) == []
