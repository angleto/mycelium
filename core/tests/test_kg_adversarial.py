"""Adversarial verification of the temporal knowledge graph (ADR-0044 + 0068).

Each test reproduces a finding from the adversarial audit of Track B; together
they pin the bi-temporal correctness, GDPR-by-provenance, and
invalidate-not-delete invariants that the prior session's note claimed but did
not all hold. Reuses the no-fixture idiom: admin_session() + signup() +
tenant_session().
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select, text  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402

from mycelium_core.ai_providers import LLMResult  # noqa: E402
from mycelium_core.db import admin_session, tenant_session  # noqa: E402
from mycelium_core.errors import DomainError  # noqa: E402
from mycelium_core.models.kg import KgEdge, KgEntity, KgEntitySource  # noqa: E402
from mycelium_core.models.note import NoteKind  # noqa: E402
from mycelium_core.services import auth, kg  # noqa: E402
from mycelium_core.services import notes as nt  # noqa: E402
from mycelium_core.services.auth import signup  # noqa: E402


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="KGADV")
    return r.org_id, r.user_id


class _FakeExtractLLM:
    model_id = "fake-llm"

    def __init__(self, payload: str) -> None:
        self._payload = payload

    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        return LLMResult(text=self._payload, tokens_in=5, tokens_out=5, model_id=self.model_id)


# ---------------------------------------------------------------------------
# B-2 -- re-asserting a superseded triple must become current ("re-hire").
# ---------------------------------------------------------------------------
async def test_reassert_superseded_triple_becomes_current() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        alice = await kg.ensure_entity(s, org_id=org, name="Alice", entity_type="person")
        a = await kg.ensure_entity(s, org_id=org, name="Acme", entity_type="organization")
        b = await kg.ensure_entity(s, org_id=org, name="Beta", entity_type="organization")
        first = await kg.add_fact(
            s,
            org_id=org,
            subject_id=alice.id,
            predicate="works_at",
            object_id=a.id,
            valid_from=dt.datetime(2018, 1, 1, tzinfo=dt.UTC),
        )
        # A -> B
        await kg.supersede_fact(
            s,
            org_id=org,
            actor_id=user,
            old_edge_id=first.id,
            subject_id=alice.id,
            predicate="works_at",
            object_id=b.id,
            valid_from=dt.datetime(2021, 1, 1, tzinfo=dt.UTC),
        )
        # find the current B edge to supersede back to A
        b_edge = (
            await s.execute(
                select(KgEdge).where(
                    KgEdge.subject_id == alice.id,
                    KgEdge.object_id == b.id,
                    KgEdge.invalidated_at.is_(None),
                    KgEdge.valid_to.is_(None),
                )
            )
        ).scalar_one()
        # B -> A again (the re-hire). Under 0067 this silently returned the
        # stale closed A row; now it must create a fresh, current A fact.
        _old, rehire = await kg.supersede_fact(
            s,
            org_id=org,
            actor_id=user,
            old_edge_id=b_edge.id,
            subject_id=alice.id,
            predicate="works_at",
            object_id=a.id,
            valid_from=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
        )
        assert rehire.id != first.id  # NOT the stale 2018 row
        assert rehire.valid_to is None  # it is the current open fact
        now = await kg.entity_facts(s, org_id=org, actor_id=user, entity_id=alice.id)
        assert {f.object_name for f in now} == {"Acme"}  # currently at Acme again


# ---------------------------------------------------------------------------
# B-3 -- a backdated / at-start supersede is rejected cleanly (no IntegrityError).
# ---------------------------------------------------------------------------
async def test_supersede_backdated_cutover_raises_domainerror() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        x = await kg.ensure_entity(s, org_id=org, name="Xena", entity_type="person")
        a = await kg.ensure_entity(s, org_id=org, name="A1", entity_type="organization")
        b = await kg.ensure_entity(s, org_id=org, name="B1", entity_type="organization")
        old = await kg.add_fact(
            s,
            org_id=org,
            subject_id=x.id,
            predicate="works_at",
            object_id=a.id,
            valid_from=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
        )
        with pytest.raises(DomainError):
            await kg.supersede_fact(
                s,
                org_id=org,
                actor_id=user,
                old_edge_id=old.id,
                subject_id=x.id,
                predicate="works_at",
                object_id=b.id,
                valid_from=dt.datetime(2015, 1, 1, tzinfo=dt.UTC),  # before old start
            )


async def test_add_fact_rejects_zero_width_window() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        x = await kg.ensure_entity(s, org_id=org, name="Zed", entity_type="person")
        a = await kg.ensure_entity(s, org_id=org, name="Zorg", entity_type="organization")
        t = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
        with pytest.raises(DomainError):
            await kg.add_fact(
                s,
                org_id=org,
                subject_id=x.id,
                predicate="at",
                object_id=a.id,
                valid_from=t,
                valid_to=t,
            )


# ---------------------------------------------------------------------------
# B-4 -- invalidate-not-delete: a direct DELETE of history is refused.
# ---------------------------------------------------------------------------
async def _raw_delete_edge(org: uuid.UUID, user: uuid.UUID, edge_id: uuid.UUID) -> None:
    async with tenant_session(str(org), str(user)) as s:
        await s.execute(text("DELETE FROM kg_edge WHERE id = :i"), {"i": str(edge_id)})
        await s.flush()


async def test_direct_delete_of_invalidated_edge_is_blocked() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await kg.ensure_entity(s, org_id=org, name="Carl", entity_type="person")
        b = await kg.ensure_entity(s, org_id=org, name="Nope", entity_type="organization")
        edge = await kg.add_fact(s, org_id=org, subject_id=a.id, predicate="at", object_id=b.id)
        edge_id = edge.id
        await kg.invalidate_fact(s, org_id=org, actor_id=user, edge_id=edge_id)
    with pytest.raises(DBAPIError):
        await _raw_delete_edge(org, user, edge_id)


async def test_direct_delete_of_current_edge_is_blocked() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await kg.ensure_entity(s, org_id=org, name="Dana", entity_type="person")
        b = await kg.ensure_entity(s, org_id=org, name="Cur", entity_type="organization")
        edge = await kg.add_fact(s, org_id=org, subject_id=a.id, predicate="at", object_id=b.id)
        edge_id = edge.id
    with pytest.raises(DBAPIError):
        await _raw_delete_edge(org, user, edge_id)


# ---------------------------------------------------------------------------
# B-1 -- GDPR erase-by-provenance actually removes KG facts (and prunes orphans).
# ---------------------------------------------------------------------------
async def test_erase_by_source_hard_deletes_facts_and_prunes_orphans() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="src", text="x"
        )
        a = await kg.ensure_entity(s, org_id=org, name="PiiPerson", entity_type="person")
        b = await kg.ensure_entity(s, org_id=org, name="PiiOrg", entity_type="organization")
        live = await kg.add_fact(
            s,
            org_id=org,
            subject_id=a.id,
            predicate="works_at",
            object_id=b.id,
            source_note_id=note.id,
        )
        # also an invalidated fact from the same note -- erasure must reach history too
        inv = await kg.add_fact(
            s,
            org_id=org,
            subject_id=a.id,
            predicate="knew",
            object_id=b.id,
            source_note_id=note.id,
        )
        await kg.invalidate_fact(s, org_id=org, actor_id=user, edge_id=inv.id)
        erased = await kg.erase_by_source(s, org_id=org, actor_id=user, source_note_id=note.id)
        assert erased == 2  # both the live and the invalidated fact are gone
        remaining = (
            await s.execute(select(func.count()).select_from(KgEdge).where(KgEdge.org_id == org))
        ).scalar_one()
        assert remaining == 0
        # orphaned entities (no remaining facts) are pruned
        ents = (
            await s.execute(
                select(func.count()).select_from(KgEntity).where(KgEntity.org_id == org)
            )
        ).scalar_one()
        assert ents == 0
        assert live.id is not None  # (no leak of the now-deleted row id check)


async def test_gdpr_erase_note_cascades_to_kg() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="n", text="body"
        )
        a = await kg.ensure_entity(s, org_id=org, name="Ghost", entity_type="person")
        b = await kg.ensure_entity(s, org_id=org, name="GhostCo", entity_type="organization")
        await kg.add_fact(
            s,
            org_id=org,
            subject_id=a.id,
            predicate="works_at",
            object_id=b.id,
            source_note_id=note.id,
        )
        await nt.gdpr_erase_note(s, org_id=org, actor_id=user, note_id=note.id)
        remaining = (
            await s.execute(select(func.count()).select_from(KgEdge).where(KgEdge.org_id == org))
        ).scalar_one()
        assert remaining == 0


# ---------------------------------------------------------------------------
# B-4b -- a note hard-delete (FK SET NULL) must NOT wedge on an invalidated fact.
# ---------------------------------------------------------------------------
async def test_note_delete_set_null_does_not_wedge_on_invalidated_fact() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="n", text="b"
        )
        note_id = note.id
        a = await kg.ensure_entity(s, org_id=org, name="Ivy", entity_type="person")
        b = await kg.ensure_entity(s, org_id=org, name="Wrong", entity_type="organization")
        edge = await kg.add_fact(
            s,
            org_id=org,
            subject_id=a.id,
            predicate="at",
            object_id=b.id,
            source_note_id=note_id,
        )
        edge_id = edge.id
        await kg.invalidate_fact(s, org_id=org, actor_id=user, edge_id=edge_id)
    # A raw note delete fires the RI ON DELETE SET NULL update on the invalidated
    # edge at trigger depth > 1: it must be allowed, not raise.
    async with tenant_session(str(org), str(user)) as s:
        await s.execute(text("DELETE FROM notes WHERE id = :i"), {"i": str(note_id)})
        await s.flush()
    async with tenant_session(str(org), str(user)) as s:
        row = (await s.execute(select(KgEdge).where(KgEdge.id == edge_id))).scalar_one()
        assert row.source_note_id is None  # provenance severed, row preserved


# ---------------------------------------------------------------------------
# B-4 cascade safety -- org teardown still cascades through the delete trigger.
# ---------------------------------------------------------------------------
async def test_org_delete_cascades_kg_even_with_invalidated_facts() -> None:
    # signup gives the user org #1; add a second org so delete is allowed.
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="Keep")
    user = r.user_id
    async with tenant_session(str(r.org_id), str(user)) as s:
        org2 = await auth.create_org_for_user(s, user_id=user, name="Doomed")
    async with tenant_session(str(org2), str(user)) as s:
        a = await kg.ensure_entity(s, org_id=org2, name="T", entity_type="person")
        b = await kg.ensure_entity(s, org_id=org2, name="U", entity_type="organization")
        e = await kg.add_fact(s, org_id=org2, subject_id=a.id, predicate="at", object_id=b.id)
        await kg.invalidate_fact(s, org_id=org2, actor_id=user, edge_id=e.id)
    # Hard-delete the org: the FK cascade deletes kg_edge/kg_entity at trigger
    # depth > 1, which the delete guard permits.
    async with tenant_session(str(r.org_id), str(user)) as s:
        await auth.delete_org_for_user(s, user_id=user, org_id=org2)
    async with admin_session() as s:
        leaked = (
            await s.execute(
                text("SELECT count(*) FROM kg_edge WHERE org_id = :o"), {"o": str(org2)}
            )
        ).scalar_one()
        assert leaked == 0


# ---------------------------------------------------------------------------
# Boundary -- as_of exactly at the cutover instant returns exactly one fact.
# ---------------------------------------------------------------------------
async def test_as_of_at_cutover_instant_is_unambiguous() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        alice = await kg.ensure_entity(s, org_id=org, name="Al", entity_type="person")
        old_c = await kg.ensure_entity(s, org_id=org, name="OldC", entity_type="organization")
        new_c = await kg.ensure_entity(s, org_id=org, name="NewC", entity_type="organization")
        old = await kg.add_fact(
            s,
            org_id=org,
            subject_id=alice.id,
            predicate="works_at",
            object_id=old_c.id,
            valid_from=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
        )
        cutover = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
        await kg.supersede_fact(
            s,
            org_id=org,
            actor_id=user,
            old_edge_id=old.id,
            subject_id=alice.id,
            predicate="works_at",
            object_id=new_c.id,
            valid_from=cutover,
        )
        eps = dt.timedelta(seconds=1)
        before = await kg.entity_facts(
            s, org_id=org, actor_id=user, entity_id=alice.id, as_of=cutover - eps
        )
        at = await kg.entity_facts(s, org_id=org, actor_id=user, entity_id=alice.id, as_of=cutover)
        assert {f.object_name for f in before} == {"OldC"}  # half-open: old still true
        assert {f.object_name for f in at} == {"NewC"}  # at the instant, new owns it


# ---------------------------------------------------------------------------
# B-5 -- transaction-time as-of reconstructs past belief.
# ---------------------------------------------------------------------------
async def test_transaction_time_as_of_reconstructs_belief() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await kg.ensure_entity(s, org_id=org, name="Bob", entity_type="person")
        b = await kg.ensure_entity(s, org_id=org, name="Corp", entity_type="organization")
        edge = await kg.add_fact(s, org_id=org, subject_id=a.id, predicate="at", object_id=b.id)
        t0 = edge.created_at
        await kg.invalidate_fact(
            s, org_id=org, actor_id=user, edge_id=edge.id, now=t0 + dt.timedelta(days=1)
        )
        # current belief: invalidated -> gone
        assert await kg.entity_facts(s, org_id=org, actor_id=user, entity_id=a.id) == []
        # belief one hour after assertion (before invalidation) -> visible
        mid = await kg.entity_facts(
            s, org_id=org, actor_id=user, entity_id=a.id, tx_as_of=t0 + dt.timedelta(hours=1)
        )
        assert {f.object_name for f in mid} == {"Corp"}
        # belief one hour before assertion -> not yet known
        pre = await kg.entity_facts(
            s, org_id=org, actor_id=user, entity_id=a.id, tx_as_of=t0 - dt.timedelta(hours=1)
        )
        assert pre == []


# ---------------------------------------------------------------------------
# B-6 -- traverse: proposed/invalidated edges never bridge a hop; cycles
# terminate; deeper nodes are reached within budget; result is deterministic.
# ---------------------------------------------------------------------------
async def test_traverse_does_not_bridge_through_proposed_or_invalidated() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        x = await kg.ensure_entity(s, org_id=org, name="X", entity_type="person")
        y = await kg.ensure_entity(s, org_id=org, name="Y", entity_type="person")
        z = await kg.ensure_entity(s, org_id=org, name="Z", entity_type="project")
        w = await kg.ensure_entity(s, org_id=org, name="W", entity_type="project")
        await kg.add_fact(s, org_id=org, subject_id=x.id, predicate="knows", object_id=y.id)
        # y -> z is PROPOSED (must not be traversable)
        await kg.add_fact(
            s,
            org_id=org,
            subject_id=y.id,
            predicate="works_on",
            object_id=z.id,
            review_state="proposed",
        )
        # y -> w is INVALIDATED (must not be traversable)
        bad = await kg.add_fact(
            s, org_id=org, subject_id=y.id, predicate="works_on", object_id=w.id
        )
        await kg.invalidate_fact(s, org_id=org, actor_id=user, edge_id=bad.id)
        walk = await kg.traverse(s, org_id=org, actor_id=user, seed_id=x.id, depth=3)
        names = {f.subject_name for f in walk} | {f.object_name for f in walk}
        assert "Y" in names
        assert "Z" not in names  # proposed edge did not bridge
        assert "W" not in names  # invalidated edge did not bridge


async def test_traverse_terminates_on_cycle_and_is_deterministic() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await kg.ensure_entity(s, org_id=org, name="Na", entity_type="person")
        b = await kg.ensure_entity(s, org_id=org, name="Nb", entity_type="person")
        await kg.add_fact(s, org_id=org, subject_id=a.id, predicate="knows", object_id=b.id)
        await kg.add_fact(s, org_id=org, subject_id=b.id, predicate="knows", object_id=a.id)
        first = await kg.traverse(s, org_id=org, actor_id=user, seed_id=a.id, depth=5)
        second = await kg.traverse(s, org_id=org, actor_id=user, seed_id=a.id, depth=5)
        assert {f.edge_id for f in first} == {f.edge_id for f in second}  # deterministic
        assert len(first) == 2  # both edges, no infinite loop


async def test_traverse_reaches_deeper_node_within_budget() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        x = await kg.ensure_entity(s, org_id=org, name="Hub", entity_type="person")
        deep = await kg.ensure_entity(s, org_id=org, name="Deep", entity_type="project")
        leaves = []
        for i in range(4):
            li = await kg.ensure_entity(s, org_id=org, name=f"L{i}", entity_type="person")
            leaves.append(li)
            await kg.add_fact(s, org_id=org, subject_id=x.id, predicate="knows", object_id=li.id)
        # second hop from the first leaf to a deeper node
        await kg.add_fact(
            s, org_id=org, subject_id=leaves[0].id, predicate="works_on", object_id=deep.id
        )
        walk = await kg.traverse(s, org_id=org, actor_id=user, seed_id=x.id, depth=2, max_edges=200)
        names = {f.subject_name for f in walk} | {f.object_name for f in walk}
        assert "Deep" in names  # the deeper node is reached (no hop-1 starvation)


# ---------------------------------------------------------------------------
# B-7 -- Unicode normalization + same-name/different-type extraction.
# ---------------------------------------------------------------------------
async def test_unicode_nfc_nfd_names_dedupe_to_one_entity() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        nfc = "Caf\u00e9"  # NFC: e-acute as one code point
        nfd = "Cafe\u0301"  # NFD: e + combining acute
        assert nfc != nfd
        e1 = await kg.ensure_entity(s, org_id=org, name=nfc, entity_type="organization")
        e2 = await kg.ensure_entity(s, org_id=org, name=nfd, entity_type="organization")
        assert e1.id == e2.id


async def test_same_name_different_type_in_extraction_stay_distinct() -> None:
    org, user = await _org()
    payload = json.dumps(
        {
            "entities": [
                {"name": "Tim", "type": "person"},
                {"name": "Apple", "type": "organization"},
                {"name": "Apple", "type": "product"},
            ],
            "relations": [{"subject": "Tim", "predicate": "works_at", "object": "Apple"}],
        }
    )
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="t", text="Tim at Apple."
        )
        await kg.extract_facts(
            s, org_id=org, actor_id=user, note_id=note.id, llm=_FakeExtractLLM(payload)
        )
        apples = (
            (
                await s.execute(
                    select(KgEntity).where(
                        KgEntity.org_id == org, KgEntity.normalized_name == "apple"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {e.entity_type for e in apples} == {"organization", "product"}  # not merged
        # the relation endpoint bound to the first typed 'Apple' (organization)
        org_apple = next(e for e in apples if e.entity_type == "organization")
        edge = (await s.execute(select(KgEdge).where(KgEdge.org_id == org))).scalar_one()
        assert edge.object_id == org_apple.id


# ---------------------------------------------------------------------------
# RLS -- cross-org traversal cannot leak another org's edges.
# ---------------------------------------------------------------------------
async def test_cross_org_traverse_isolation() -> None:
    org_a, user_a = await _org()
    org_b, user_b = await _org()
    async with tenant_session(str(org_a), str(user_a)) as s:
        a1 = await kg.ensure_entity(s, org_id=org_a, name="A1", entity_type="person")
        a2 = await kg.ensure_entity(s, org_id=org_a, name="A2", entity_type="person")
        await kg.add_fact(s, org_id=org_a, subject_id=a1.id, predicate="knows", object_id=a2.id)
        seed = a1.id
    async with tenant_session(str(org_b), str(user_b)) as s:
        walk = await kg.traverse(s, org_id=org_b, actor_id=user_b, seed_id=seed, depth=3)
        assert walk == []  # org B sees none of org A's facts


# ---------------------------------------------------------------------------
# 0069 -- GDPR erase reaches edge-less extracted entities (residual fix).
# ---------------------------------------------------------------------------
async def _n_entities(s: object, org: uuid.UUID) -> int:
    return (
        await s.execute(  # type: ignore[attr-defined]
            select(func.count()).select_from(KgEntity).where(KgEntity.org_id == org)
        )
    ).scalar_one()


async def test_gdpr_erase_removes_edge_less_extracted_entity() -> None:
    """An entity extracted into entities[] but never used in a relation has no
    kg_edge; the edge-orphan prune alone never reached it. With provenance links
    (0069), GDPR note-erase now deletes it."""
    org, user = await _org()
    payload = json.dumps({"entities": [{"name": "Solo", "type": "person"}], "relations": []})
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="t", text="Solo."
        )
        res = await kg.extract_facts(
            s, org_id=org, actor_id=user, note_id=note.id, llm=_FakeExtractLLM(payload)
        )
        assert res.facts == 0 and res.entities == 1  # edge-less entity
        assert await _n_entities(s, org) == 1
        # provenance link recorded
        n_links = (
            await s.execute(
                select(func.count()).select_from(KgEntitySource).where(KgEntitySource.org_id == org)
            )
        ).scalar_one()
        assert n_links == 1
        await nt.gdpr_erase_note(s, org_id=org, actor_id=user, note_id=note.id)
        assert await _n_entities(s, org) == 0  # the edge-less PII entity is gone


async def test_gdpr_erase_keeps_entity_shared_by_another_note() -> None:
    """An entity sourced by two notes survives erasing ONE of them (the other's
    provenance + facts retain it); an entity sourced only by the erased note is
    deleted."""
    org, user = await _org()
    p_a = json.dumps(
        {
            "entities": [
                {"name": "Tina", "type": "person"},
                {"name": "Acme", "type": "organization"},
            ],
            "relations": [{"subject": "Tina", "predicate": "works_at", "object": "Acme"}],
        }
    )
    p_b = json.dumps(
        {
            "entities": [{"name": "Tina", "type": "person"}, {"name": "Bob", "type": "person"}],
            "relations": [{"subject": "Tina", "predicate": "knows", "object": "Bob"}],
        }
    )
    async with tenant_session(str(org), str(user)) as s:
        note_a = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="A", text="Tina at Acme."
        )
        note_b = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="B", text="Tina knows Bob."
        )
        await kg.extract_facts(
            s, org_id=org, actor_id=user, note_id=note_a.id, llm=_FakeExtractLLM(p_a)
        )
        await kg.extract_facts(
            s, org_id=org, actor_id=user, note_id=note_b.id, llm=_FakeExtractLLM(p_b)
        )
        assert await _n_entities(s, org) == 3  # Tina, Acme, Bob
        # Erase note A: Acme (only A) goes; Tina (also in B) stays; Bob untouched.
        await nt.gdpr_erase_note(s, org_id=org, actor_id=user, note_id=note_a.id)
        names = {
            e.name
            for e in (await s.execute(select(KgEntity).where(KgEntity.org_id == org)))
            .scalars()
            .all()
        }
        assert names == {"Tina", "Bob"}  # Acme erased, Tina retained via note B
        # Erase note B too: Tina + Bob now have no provenance left -> gone.
        await nt.gdpr_erase_note(s, org_id=org, actor_id=user, note_id=note_b.id)
        assert await _n_entities(s, org) == 0
