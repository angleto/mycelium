"""Temporal knowledge graph (ADR-0044, Track B).

Asserts: entity dedupe/resolution, metered LLM extraction + defensive parse,
idempotent facts, bi-temporal supersede (temporal update keeps history +
as-of) and invalidate (tombstone, frozen by the DB trigger), the
born-proposed review gate, multi-hop traversal, and RLS org isolation.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import uuid
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402

from mycelium_core.ai_providers import LLMResult, set_llm_override  # noqa: E402
from mycelium_core.db import admin_session, tenant_session  # noqa: E402
from mycelium_core.models.billing import UsageRecord  # noqa: E402
from mycelium_core.models.kg import KgEdge, KgEntity  # noqa: E402
from mycelium_core.models.note import NoteKind  # noqa: E402
from mycelium_core.services import billing, kg  # noqa: E402
from mycelium_core.services import notes as nt  # noqa: E402
from mycelium_core.services.auth import signup  # noqa: E402


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="KG")
    return r.org_id, r.user_id


class _FakeExtractLLM:
    """Returns a fixed JSON triples envelope (FakeLLM echoes prose, not JSON)."""

    model_id = "fake-llm"

    def __init__(self, payload: str) -> None:
        self._payload = payload

    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        return LLMResult(text=self._payload, tokens_in=5, tokens_out=5, model_id=self.model_id)


async def _seed_billing(s: object, org: uuid.UUID, user: uuid.UUID) -> None:
    await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(1000))  # type: ignore[arg-type]
    await billing.upsert_rate_card(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        model_id="fake-llm",
        provider="local",
        values={"credits_per_input": Decimal("0.001"), "credits_per_output": Decimal("0.001")},
    )


async def test_ensure_entity_dedupes_on_normalized_name() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        e1 = await kg.ensure_entity(
            s, org_id=org, name="  Acme   Corp ", entity_type="organization"
        )
        e2 = await kg.ensure_entity(s, org_id=org, name="acme corp", entity_type="organization")
        assert e1.id == e2.id  # whitespace + case fold to the same canonical node
        e3 = await kg.ensure_entity(s, org_id=org, name="acme corp", entity_type="project")
        assert e3.id != e1.id  # a different type is a different entity


async def test_extract_creates_entities_and_facts_idempotently() -> None:
    org, user = await _org()
    payload = json.dumps(
        {
            "entities": [
                {"name": "Bob", "type": "person"},
                {"name": "Globex", "type": "organization"},
            ],
            "relations": [
                {
                    "subject": "Bob",
                    "predicate": "works at",  # normalized -> works_at
                    "object": "Globex",
                    "valid_from": "2020-01-01",
                }
            ],
        }
    )
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="b", text="Bob works at Globex."
        )
        res = await kg.extract_facts(
            s, org_id=org, actor_id=user, note_id=note.id, llm=_FakeExtractLLM(payload)
        )
        assert res.entities == 2
        assert res.facts == 1
        edge = (await s.execute(select(KgEdge).where(KgEdge.id == res.edge_ids[0]))).scalar_one()
        assert edge.predicate == "works_at"  # open vocab normalized
        assert edge.valid_from == dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
        assert edge.source_note_id == note.id
        # Re-extraction is idempotent on the triple: still exactly one edge.
        await kg.extract_facts(
            s, org_id=org, actor_id=user, note_id=note.id, llm=_FakeExtractLLM(payload)
        )
        n_edges = (
            await s.execute(select(func.count()).select_from(KgEdge).where(KgEdge.org_id == org))
        ).scalar_one()
        assert n_edges == 1


async def test_extract_meters_through_the_per_org_seam() -> None:
    org, user = await _org()
    payload = json.dumps(
        {
            "entities": [
                {"name": "Ann", "type": "person"},
                {"name": "Initech", "type": "organization"},
            ],
            "relations": [{"subject": "Ann", "predicate": "works_at", "object": "Initech"}],
        }
    )
    set_llm_override(lambda: _FakeExtractLLM(payload))
    try:
        async with tenant_session(str(org), str(user)) as s:
            await _seed_billing(s, org, user)
            note = await nt.create_note(
                s,
                org_id=org,
                actor_id=user,
                kind=NoteKind.text,
                title="bio",
                text="Ann at Initech.",
            )
            res = await kg.extract_facts(s, org_id=org, actor_id=user, note_id=note.id)
            assert res.facts == 1
            assert res.model_id == "fake-llm"
            rec = (
                await s.execute(
                    select(UsageRecord).where(
                        UsageRecord.operation_id == f"kg_extract:{org}:{note.id}"
                    )
                )
            ).scalar_one()
            assert rec.op == "extract"
    finally:
        set_llm_override(None)


async def test_extract_tolerates_non_json_output() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="x", text="some prose"
        )
        # A model that returns prose (or fenced JSON) must not break the pipeline.
        res = await kg.extract_facts(
            s,
            org_id=org,
            actor_id=user,
            note_id=note.id,
            llm=_FakeExtractLLM("sorry, no JSON here"),
        )
        assert res.facts == 0


async def test_supersede_keeps_history_and_as_of_query() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        alice = await kg.ensure_entity(s, org_id=org, name="Alice", entity_type="person")
        old_corp = await kg.ensure_entity(s, org_id=org, name="OldCorp", entity_type="organization")
        new_corp = await kg.ensure_entity(s, org_id=org, name="NewCorp", entity_type="organization")
        old = await kg.add_fact(
            s,
            org_id=org,
            subject_id=alice.id,
            predicate="works_at",
            object_id=old_corp.id,
            valid_from=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
        )
        cutover = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
        old2, new = await kg.supersede_fact(
            s,
            org_id=org,
            actor_id=user,
            old_edge_id=old.id,
            subject_id=alice.id,
            predicate="works_at",
            object_id=new_corp.id,
            valid_from=cutover,
        )
        await s.refresh(old2)
        # Temporal update, NOT a retraction: the old fact stays believed but
        # its valid window is closed and chained to the replacement.
        assert old2.invalidated_at is None
        assert old2.valid_to == cutover
        assert old2.superseded_by_edge_id == new.id
        # As-of 2022 -> the fact true then (OldCorp); as-of now -> NewCorp.
        past = await kg.entity_facts(
            s,
            org_id=org,
            actor_id=user,
            entity_id=alice.id,
            as_of=dt.datetime(2022, 1, 1, tzinfo=dt.UTC),
        )
        assert {f.object_name for f in past} == {"OldCorp"}
        now = await kg.entity_facts(s, org_id=org, actor_id=user, entity_id=alice.id)
        assert {f.object_name for f in now} == {"NewCorp"}


async def _update_confidence(org: uuid.UUID, user: uuid.UUID, edge_id: uuid.UUID) -> None:
    """Mutate an edge in a fresh session (used to prove the freeze trigger)."""
    async with tenant_session(str(org), str(user)) as s:
        frozen = (await s.execute(select(KgEdge).where(KgEdge.id == edge_id))).scalar_one()
        frozen.confidence = Decimal("0.5")
        await s.flush()


async def test_invalidate_is_a_frozen_tombstone() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await kg.ensure_entity(s, org_id=org, name="Carol", entity_type="person")
        b = await kg.ensure_entity(s, org_id=org, name="Wrongo", entity_type="organization")
        edge = await kg.add_fact(
            s, org_id=org, subject_id=a.id, predicate="works_at", object_id=b.id
        )
        edge_id = edge.id
        await kg.invalidate_fact(s, org_id=org, actor_id=user, edge_id=edge_id)
        # Tombstoned -> gone from every read.
        assert await kg.entity_facts(s, org_id=org, actor_id=user, entity_id=a.id) == []
    # invalidate-not-delete: the DB trigger freezes an invalidated row.
    with pytest.raises(DBAPIError):
        await _update_confidence(org, user, edge_id)


async def test_proposed_facts_withheld_until_approved() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        x = await kg.ensure_entity(s, org_id=org, name="X", entity_type="person")
        y = await kg.ensure_entity(s, org_id=org, name="Y", entity_type="organization")
        edge = await kg.add_fact(
            s,
            org_id=org,
            subject_id=x.id,
            predicate="member_of",
            object_id=y.id,
            review_state="proposed",
        )
        assert await kg.entity_facts(s, org_id=org, actor_id=user, entity_id=x.id) == []
        await kg.approve_fact(s, org_id=org, actor_id=user, edge_id=edge.id)
        facts = await kg.entity_facts(s, org_id=org, actor_id=user, entity_id=x.id)
        assert len(facts) == 1


async def test_traverse_multi_hop_reaches_shared_neighbor() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        x = await kg.ensure_entity(s, org_id=org, name="Xavier", entity_type="person")
        y = await kg.ensure_entity(s, org_id=org, name="Yara", entity_type="person")
        proj = await kg.ensure_entity(s, org_id=org, name="Atlas", entity_type="project")
        await kg.add_fact(s, org_id=org, subject_id=x.id, predicate="works_on", object_id=proj.id)
        await kg.add_fact(s, org_id=org, subject_id=y.id, predicate="works_on", object_id=proj.id)
        one = await kg.entity_facts(s, org_id=org, actor_id=user, entity_id=x.id)
        assert {f.object_name for f in one} == {"Atlas"}
        two = await kg.traverse(s, org_id=org, actor_id=user, seed_id=x.id, depth=2)
        names = {f.subject_name for f in two} | {f.object_name for f in two}
        assert {"Xavier", "Yara", "Atlas"} <= names


async def test_rls_isolates_kg_between_orgs() -> None:
    org_a, user_a = await _org()
    org_b, user_b = await _org()
    async with tenant_session(str(org_a), str(user_a)) as s:
        ent = await kg.ensure_entity(s, org_id=org_a, name="SecretCorp", entity_type="organization")
        ent_id = ent.id
    async with tenant_session(str(org_b), str(user_b)) as s:
        leaked = (
            await s.execute(select(KgEntity).where(KgEntity.id == ent_id))
        ).scalar_one_or_none()
        assert leaked is None  # RLS hides org A's row from org B
        assert await kg.search_entities(s, org_id=org_b, actor_id=user_b, query="SecretCorp") == []
