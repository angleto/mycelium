"""The ADR-0073 retrieval A/B (decision 8): written references as graph
evidence, measured on the WS-EVAL synthetic workspace.

FakeEmbedder and a tiny scale: these tests pin the MACHINERY (the corpus
transform, the ingest of written references through the real write path,
the per-call knob reaching the graph walk, the paired verdict), never a
quality threshold. The decision itself is the recorded run of
``scripts/eval_written_references_ab.py`` at the pre-registered scale.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.models.body_reference import BodyReference, BodyReferenceSource
from mycelium_core.models.note import Note
from mycelium_core.models.note_link import NoteNoteLink
from mycelium_core.models.note_part import NotePart
from mycelium_core.services import eval_baselines as base
from mycelium_core.services import graph_local
from mycelium_core.services.auth import signup
from mycelium_core.services.eval_queries import build_queries
from mycelium_core.services.eval_workspace import (
    Workspace,
    generate_workspace,
    ingest_workspace,
    write_links_as_references,
)

_SEED = 4242
_SCALE = 150
_FAKE = FakeEmbedder()


@pytest.fixture()
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


def _related_note_links(ws: Workspace) -> set[tuple[str, str]]:
    kind = {u.unit_id: u.unit_kind for u in ws.units}
    return {
        (u.unit_id, link["to"])
        for u in ws.units
        if u.unit_kind == "note"
        for link in u.links
        if link["kind"] == "related" and kind.get(link["to"]) == "note"
    }


# ---------------------------------------------------------------- transform


def test_transform_moves_the_registered_fraction_and_nothing_else() -> None:
    ws = generate_workspace(seed=_SEED, scale=_SCALE)
    before = _related_note_links(ws)
    assert before, "the generator must produce related links between notes"
    out = write_links_as_references(ws)
    moved = {(u.unit_id, t) for u in out.units for t in u.written_refs}
    kept = _related_note_links(out)
    assert moved | kept == before and not (moved & kept)
    assert 0 < len(moved) < len(before)
    # Every other link, and every text, is untouched.
    for a, b in zip(ws.units, out.units, strict=True):
        assert a.text == b.text
        assert [x for x in a.links if x["kind"] != "related"] == [
            x for x in b.links if x["kind"] != "related"
        ]
    # The input is not modified, and the draw is deterministic.
    assert _related_note_links(ws) == before
    assert all(not u.written_refs for u in ws.units)
    assert write_links_as_references(ws) == out


def test_transform_extremes() -> None:
    ws = generate_workspace(seed=_SEED, scale=_SCALE)
    assert write_links_as_references(ws, fraction=0.0).units == ws.units
    every = write_links_as_references(ws, fraction=1.0)
    assert _related_note_links(every) == set()
    with pytest.raises(ValueError):
        write_links_as_references(ws, fraction=1.5)


# ---------------------------------------------------------------- ingest + knob


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"adr0073-{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="ADR0073-AB",
        )
    return r.org_id, r.user_id


async def _ingested(
    ws: Workspace,
) -> tuple[uuid.UUID, uuid.UUID, Any]:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        ingest = await ingest_workspace(s, org_id=org, actor_id=user, ws=ws)
    return org, user, ingest


async def test_written_references_are_ingested_as_a_person_writes_them(
    _embedder: None,
) -> None:
    ws = write_links_as_references(generate_workspace(seed=_SEED, scale=_SCALE))
    org, user, ingest = await _ingested(ws)
    note_of = {uid: uuid.UUID(m["note_id"]) for uid, m in ingest.units.items() if "note_id" in m}
    expected = {
        (note_of[u.unit_id], note_of[t]) for u in ws.units for t in u.written_refs if t in note_of
    }
    async with tenant_session(str(org), str(user)) as s:
        rows = (
            await s.execute(
                select(NotePart.note_id, BodyReference.target_id, BodyReference.status)
                .join(BodyReferenceSource, BodyReferenceSource.id == BodyReference.source_id)
                .join(NotePart, NotePart.id == BodyReferenceSource.note_part_id)
                .where(BodyReference.org_id == org)
            )
        ).all()
        links = (
            await s.execute(
                select(NoteNoteLink.parent_note_id, NoteNoteLink.child_note_id).where(
                    NoteNoteLink.org_id == org, NoteNoteLink.kind == "related"
                )
            )
        ).all()
        archived = {
            n.id: bool(n.is_archived)
            for n in (await s.execute(select(Note).where(Note.org_id == org))).scalars()
        }
    projected = {(src, tgt) for src, tgt, status in rows if status == "resolved"}
    assert expected and expected <= projected
    # A written relation is not also a typed one, unless the generator drew
    # the same pair twice (one written, one kept).
    typed = {frozenset(p) for p in links}
    kept = {
        frozenset((note_of[u.unit_id], note_of[x["to"]]))
        for u in ws.units
        if u.unit_kind == "note"
        for x in u.links
        if x["kind"] == "related" and x["to"] in note_of
    }
    assert {frozenset(p) for p in expected if frozenset(p) not in kept}.isdisjoint(typed)
    # A citing note that the generator archives is still archived.
    for u in ws.units:
        if u.unit_kind == "note" and u.written_refs:
            assert archived[note_of[u.unit_id]] is u.archived


async def test_the_knob_decides_whether_the_walk_reads_written_references(
    _embedder: None,
) -> None:
    ws = write_links_as_references(generate_workspace(seed=_SEED, scale=_SCALE), fraction=1.0)
    org, user, ingest = await _ingested(ws)
    note_of = {uid: uuid.UUID(m["note_id"]) for uid, m in ingest.units.items() if "note_id" in m}
    citing = next(
        u for u in ws.units if u.unit_kind == "note" and u.written_refs and not u.archived
    )
    src, tgt = note_of[citing.unit_id], note_of[citing.written_refs[0]]
    async with tenant_session(str(org), str(user)) as s:
        off = await graph_local.local_edges(s, org_id=org, note_id=src, written_references=False)
        on = await graph_local.local_edges(s, org_id=org, note_id=src, written_references=True)
    assert on.get(tgt, 0.0) > off.get(tgt, 0.0)


async def test_ab_end_to_end(_embedder: None) -> None:
    ws = write_links_as_references(generate_workspace(seed=_SEED, scale=_SCALE))
    queries = build_queries(ws, seed=_SEED + 1000)
    org, user, ingest = await _ingested(ws)
    async with tenant_session(str(org), str(user)) as s:
        runs = await base.run_written_reference_ab(
            s,
            org_id=org,
            actor_id=user,
            ws=ws,
            ingest=ingest,
            records=queries,
            embedder=_FAKE,
            k=10,
        )
    by = {r.system: r for r in runs}
    assert set(by) == {base.SYSTEM_GRAPH, base.SYSTEM_GRAPH_WRITTEN}
    a, b = by[base.SYSTEM_GRAPH].records, by[base.SYSTEM_GRAPH_WRITTEN].records
    assert len(a) == len(b) > 0
    assert [r["qid"] for r in a] == [r["qid"] for r in b]
    assert all({"graph_ran", "graph_contributed"} <= set(r) for r in a + b)
    v = base.written_reference_verdict(runs, n_resamples=200)
    assert v.n > 0
    assert v.recall_ci[0] <= v.d_recall <= v.recall_ci[1]
    assert v.mrr_ci[0] <= v.d_mrr <= v.mrr_ci[1]


# ---------------------------------------------------------------- verdict


def _run(system: str, ranks: list[int | None]) -> base.SystemRun:
    recs = [
        {
            "qid": f"q{i}",
            "fact_id": f"f{i}",
            "category": "single_fact",
            "rank": r,
            "impossible": False,
        }
        for i, r in enumerate(ranks)
    ]
    return base.SystemRun(system=system, proxy=False, records=recs, skipped_non_note=0)


def test_verdict_passes_on_parity_and_fails_on_a_clear_loss() -> None:
    same = [1, 2, None, 1, 3] * 8
    v = base.written_reference_verdict(
        [_run(base.SYSTEM_GRAPH, same), _run(base.SYSTEM_GRAPH_WRITTEN, same)], n_resamples=200
    )
    assert v.passes and v.d_recall == 0.0 and v.d_mrr == 0.0
    worse = [None if r == 1 else r for r in same]
    v = base.written_reference_verdict(
        [_run(base.SYSTEM_GRAPH, same), _run(base.SYSTEM_GRAPH_WRITTEN, worse)], n_resamples=200
    )
    assert not v.passes and v.recall_ci[1] < 0.0


def test_verdict_needs_the_written_arm() -> None:
    with pytest.raises(ValueError):
        base.written_reference_verdict([_run(base.SYSTEM_GRAPH, [1])])
    assert dataclasses.is_dataclass(base.WrittenReferenceVerdict)
