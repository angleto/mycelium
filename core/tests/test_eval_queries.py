"""WS-EVAL T2 (task 4a2670ac): the adversarial query generator's protocol
obligations as tests -- determinism, category invariants (anti-recency,
registry-verified impossibles, perimeter contract, per-fact cap, competitive
fan-out), template disjointness vs T1, the hardness-gate machinery on a real
ingested workspace, and the human-anchor round trip."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from _fake_embedder import FakeEmbedder

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.services import eval_queries as qgen
from mycelium_core.services import eval_workspace as wsgen
from mycelium_core.services.auth import signup

_SEED = 4242
_QSEED = 777
_SCALE = 150


@pytest.fixture()
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


def _ws() -> wsgen.Workspace:
    return wsgen.generate_workspace(seed=_SEED, scale=_SCALE)


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="WSQ",
        )
    return r.org_id, r.user_id


def test_determinism(tmp_path: Path) -> None:
    ws = _ws()
    a = qgen.build_queries(ws, seed=_QSEED)
    b = qgen.build_queries(ws, seed=_QSEED)
    assert [vars(r) for r in a] == [vars(r) for r in b]
    c = qgen.build_queries(ws, seed=_QSEED + 1)
    assert [vars(r) for r in a] != [vars(r) for r in c]
    manifest = qgen.write_query_artifacts(a, tmp_path)
    assert manifest["total"] == len(a)
    reloaded = [json.loads(line) for line in (tmp_path / "queries.jsonl").read_text().splitlines()]
    assert len(reloaded) == len(a)


def test_category_invariants() -> None:
    ws = _ws()
    records = qgen.build_queries(ws, seed=_QSEED)
    by_cat: dict[str, list[qgen.QueryRecord]] = {}
    for r in records:
        by_cat.setdefault(r.category, []).append(r)
    facts = {f.fact_id: f for f in ws.facts}
    realized = {(f.entity_id, f.attribute) for f in ws.facts}
    units = {u.unit_id: u for u in ws.units}

    # The core categories must exist at protocol scales (deterministic seed).
    for cat in (
        "collision",
        "freshness",
        "as_of_previous",
        "distributed",
        "impossible",
        "perimeter",
        "erasure",
    ):
        assert by_cat.get(cat), f"category {cat} missing at scale {_SCALE}"

    # Anti-recency (§3): the as_of gold IS the stale unit; the fresh unit is
    # the tracked distractor a recency prior would wrongly serve.
    for r in by_cat["as_of_previous"]:
        f = facts[r.fact_id or ""]
        assert r.gold_unit_ids == [f.stale_unit_id]
        assert r.distractor_unit_ids == f.gold_unit_ids[:1]

    # Freshness: current gold, stale distractor.
    for r in by_cat["freshness"]:
        f = facts[r.fact_id or ""]
        assert r.gold_unit_ids == f.gold_unit_ids[:1]
        assert r.distractor_unit_ids == [f.stale_unit_id]

    # Impossible = near-miss verified against the REGISTRY (any category).
    for r in by_cat["impossible"]:
        assert r.expected_empty and r.fact_id is None and not r.gold_unit_ids
    ent_by_name = {e.name: e for e in ws.entities}
    for r in by_cat["impossible"]:
        holders = [e for name, e in ent_by_name.items() if name in r.query_text]
        assert holders, f"impossible query names no known entity: {r.query_text!r}"
        # The asked attribute must be UNREALIZED for the named entity: some
        # schema attribute of a named entity has no fact of any category, and
        # its question-side phrasing appears in the text (near-miss, §3).
        assert any(
            (e.entity_id, a) not in realized
            and qgen._qlabel(e.entity_type, a, r.lang) in r.query_text
            for e in holders
            for a in wsgen._SCHEMA[e.entity_type]
        ), f"impossible query does not match an unrealized attribute: {r.query_text!r}"

    # Perimeter: cross-project contract fields.
    for r in by_cat["perimeter"]:
        assert r.expected_empty
        assert r.home_project and r.context_project
        assert r.home_project != r.context_project
        assert units[r.gold_unit_ids[0]].project == r.home_project

    # Erasure: the linkage contract T3 consumes.
    for r in by_cat["erasure"]:
        assert r.erase_unit_id == r.gold_unit_ids[0]
        assert r.degrades_after_erase

    # Multi-hop: competitive fan-out recorded and hop units emitted.
    for r in [*by_cat.get("multi_hop_2", []), *by_cat.get("multi_hop_3", [])]:
        assert (r.fan_out or 0) >= 2
        assert r.hop_unit_ids
        assert r.gold_unit_ids

    # Per-fact cap (§3).
    per_fact: dict[str, int] = {}
    for r in records:
        if r.fact_id:
            per_fact[r.fact_id] = per_fact.get(r.fact_id, 0) + 1
    assert max(per_fact.values()) <= qgen.MAX_QUERIES_PER_FACT

    # Language inversion actually happens (pre-registered fraction > 0).
    inverted = [r for r in records if r.lang_inverted and r.fact_id]
    assert inverted
    for r in inverted[:10]:
        assert r.lang != facts[r.fact_id or ""].lang


def test_template_disjointness_vs_t1() -> None:
    """No surface form is shared between the corpus realizations (T1) and
    the query frames/phrasings (T2) -- mismatch by construction (§3)."""
    assert qgen.t1_template_strings() & qgen.t2_template_strings() == set()


async def test_hardness_gate_machinery(_embedder: None) -> None:
    """Generate -> ingest -> score gold vs near-miss distractors on both
    channels. CI asserts the MACHINERY (rows, channels, report shape); the
    >=50% threshold gates real corpora at T5, not this tiny workspace."""
    ws = _ws()
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        ingest = await wsgen.ingest_workspace(s, org_id=org, actor_id=user, ws=ws)
    records = [
        r
        for r in qgen.build_queries(ws, seed=_QSEED)
        if r.category in ("collision", "freshness") and r.distractor_unit_ids
    ][:12]
    assert records
    async with tenant_session(str(org), str(user)) as s:
        report = await qgen.compute_hardness(
            s, org_id=org, ingest=ingest, records=records, embedder=FakeEmbedder()
        )
    assert report.rows, "hardness rows must be computed for note-backed queries"
    for row in report.rows:
        assert row.gold_lex >= 0.0 and row.best_distractor_lex >= 0.0
        assert -1.0 <= row.gold_dense <= 1.0
        assert row.competitive == (row.distractor_beats_gold or row.distractor_in_lexical_top10)
    assert 0.0 <= report.fraction_competitive <= 1.0
    assert report.threshold == qgen.HARDNESS_MIN_COMPETITIVE_FRACTION


def test_human_anchor_roundtrip(tmp_path: Path) -> None:
    ws = _ws()
    pack = tmp_path / "reviewer_pack.md"
    gold_count = sum(1 for f in ws.facts if f.queryable and f.gold_unit_ids)
    n = qgen.export_reviewer_pack(ws, pack, seed=_QSEED, max_facts=20)
    body = pack.read_text(encoding="utf-8")
    assert n == min(20, gold_count)
    # Registry only: no corpus surface text may leak into the pack.
    for filler in wsgen._FILLER["it"] + wsgen._FILLER["en"]:
        assert filler not in body
    target = next(f for f in ws.facts if f.queryable and f.gold_unit_ids and not f.distributed)
    human = tmp_path / "human_queries.jsonl"
    human.write_text(
        json.dumps({"fact_id": target.fact_id, "query_text": "dove trovo questo dato?"}) + "\n",
        encoding="utf-8",
    )
    (rec,) = qgen.import_human_queries(human, ws)
    assert rec.category == "human_anchor"
    assert rec.gold_unit_ids == target.gold_unit_ids[:1]
    assert rec.generation["provider"] == "human"
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"fact_id": "fact-99999", "query_text": "x"}) + "\n")
    with pytest.raises(ValueError, match="unknown or non-gold"):
        qgen.import_human_queries(bad, ws)
