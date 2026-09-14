"""Fase 4 of the search-informed graph (task 561c6aca): the gated,
dark-by-default graph-proximity source in the main retrieval.

Two layers of proof:

- UNIT (``GraphProximityStage`` directly): the mechanism -- a note LINKED
  to a seed but found by no base branch is injected with a ``graph`` RRF
  score and ``provenance='graph'``; a note already in the pool is boosted,
  never duplicated; the gate skips a short query / a thin pool.
- END-TO-END (``memory.retrieve``/``retrieve_with_meta``): the wiring --
  off is byte-identical to today (the default), on runs the stage and
  reports ``graph_contributed`` in ``RetrievalMeta``; ``run_eval`` threads
  the knob so the publishable A/B (recall_at_k on vs off) runs through the
  same harness.

Note on the pipeline: ``RelativeFloorStage`` cuts a low graph-only
candidate when a strong lexical hit dominates (graph is a NUDGE, not an
override) -- so the graph candidate's SURVIVAL to top-k is a ranking
question the real-corpus A/B measures, not a CI invariant. CI pins the
INJECTION (``graph_contributed``) and the mechanism, which are floor-
independent. The publishable command is documented at the bottom.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from _fake_embedder import FakeEmbedder  # noqa: E402
from sqlalchemy import select, true  # noqa: E402

from mycelium_core.config import get_settings  # noqa: E402
from mycelium_core.db import admin_session, tenant_session  # noqa: E402
from mycelium_core.embedder import set_embedder_override  # noqa: E402
from mycelium_core.models.note import NoteKind  # noqa: E402
from mycelium_core.models.note_link import NoteNoteLink  # noqa: E402
from mycelium_core.models.note_part_index_pointer import NotePartIndexPointer  # noqa: E402
from mycelium_core.services import eval_offline, memory  # noqa: E402
from mycelium_core.services import notes as nt  # noqa: E402
from mycelium_core.services.auth import signup  # noqa: E402
from mycelium_core.services.eval_offline import GoldCase  # noqa: E402
from mycelium_core.services.retrieval.stages.graph_proximity import (  # noqa: E402
    GRAPH_PROVENANCE,
    GraphGate,
    GraphProximityStage,
)
from mycelium_core.services.retrieval.types import Candidate, RetrievalContext  # noqa: E402

_FAKE = FakeEmbedder()


@pytest.fixture(autouse=True)
def _embedder() -> object:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


async def _org(name: str = "GRF4") -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name=name,
        )
    return r.org_id, r.user_id


async def _note(s: object, org: uuid.UUID, user: uuid.UUID, title: str, text: str) -> uuid.UUID:
    n = await nt.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title=title,
        text=text,
    )
    return n.id


async def _link(s: object, org: uuid.UUID, a: uuid.UUID, b: uuid.UUID, kind: str) -> None:
    s.add(NoteNoteLink(org_id=org, parent_note_id=a, child_note_id=b, kind=kind))  # type: ignore[attr-defined]
    await s.flush()  # type: ignore[attr-defined]


async def _set_semantic_floor(s: object, org: uuid.UUID, value: float) -> None:
    """Raise the org's dense-similarity floor (read live by ``mem.retrieve``).
    Under FakeEmbedder the dense branch (floor 0) would otherwise surface
    EVERY note, so a zero-overlap note is never truly 'unreachable'; a
    positive floor excludes it from the base branches -- the real-corpus
    condition the graph source addresses. Runs on the tenant session (RLS
    needs the org GUC set); mirrors test_f6_memory."""
    from sqlalchemy import update as sa_update

    from mycelium_core.models.organization import Organization

    await s.execute(  # type: ignore[attr-defined]
        sa_update(Organization)
        .where(Organization.id == org)
        .values(settings={memory.SEMANTIC_MIN_SIM_KEY: value})
    )


async def _blob_of(s: object, org: uuid.UUID, note_id: uuid.UUID) -> uuid.UUID:
    """The note's indexed part blob (create_note indexes per part at commit;
    read it back in a fresh session via the pointer)."""
    return (
        await s.execute(  # type: ignore[attr-defined]
            select(NotePartIndexPointer.blob_id).where(
                NotePartIndexPointer.org_id == org, NotePartIndexPointer.note_id == note_id
            )
        )
    ).scalar_one()


def _ctx(s: object, org: uuid.UUID, user: uuid.UUID) -> RetrievalContext:
    return RetrievalContext(
        session=s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        project_id=None,
        operation_id="graph-unit",
        embedder=_FAKE,
        project_pred=true(),
        tag_clauses=(),
        query_embedding=None,
        extras={},
    )


# ---------------------------------------------------------------- UNIT


async def test_stage_injects_linked_note_with_graph_provenance() -> None:
    """A note LINKED to a seed but not in the candidate pool is injected
    with ``provenance='graph'`` and a ``graph`` branch score; the seed is
    not re-added; ``graph_diag`` reports the contribution."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        seed = await _note(s, org, user, "seed", "alpha beta gamma seed body")
        linked = await _note(s, org, user, "linked", "carbonara pancetta pecorino unrelated")
        await _link(s, org, seed, linked, "related")
    # Fresh session so the deferred per-part indexing has landed.
    async with tenant_session(str(org), str(user)) as s:
        seed_blob = await _blob_of(s, org, seed)
        linked_blob = await _blob_of(s, org, linked)
        # A pool of 5 (gate default min_candidates), seed strongest.
        pool = [Candidate(blob_id=seed_blob, score=1.0, scores_by_stage={"rrf": 1.0})]
        pool += [Candidate(blob_id=uuid.uuid4(), score=0.5 - i * 0.05) for i in range(4)]
        ctx = _ctx(s, org, user)
        out = await GraphProximityStage().run("alpha beta gamma", ctx, pool)

        by_blob = {c.blob_id: c for c in out}
        assert linked_blob in by_blob, "linked note should be injected by the graph source"
        injected = by_blob[linked_blob]
        assert injected.provenance == GRAPH_PROVENANCE
        assert "graph" in injected.scores_by_stage
        assert injected.score > 0.0
        # seed present exactly once (bounded walk excludes the seed).
        assert sum(1 for c in out if c.blob_id == seed_blob) == 1
        assert ctx.extras["graph_diag"] == {"ran": True, "contributed": 1}


async def test_stage_boosts_existing_candidate_without_duplicating() -> None:
    """A graph-reached note that is ALSO already in the pool is boosted
    (its aggregate grows, a ``graph`` rank is recorded) but not duplicated
    and not re-stamped 'graph' (it was a live hit first)."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        seed = await _note(s, org, user, "seed", "alpha beta gamma seed")
        nb = await _note(s, org, user, "nb", "delta epsilon zeta")
        await _link(s, org, seed, nb, "related")
    async with tenant_session(str(org), str(user)) as s:
        seed_blob = await _blob_of(s, org, seed)
        nb_blob = await _blob_of(s, org, nb)
        pool = [
            Candidate(blob_id=seed_blob, score=1.0, scores_by_stage={"rrf": 1.0}),
            Candidate(blob_id=nb_blob, score=0.3, scores_by_stage={"semantic": 5, "rrf": 0.3}),
        ] + [Candidate(blob_id=uuid.uuid4(), score=0.1) for _ in range(3)]
        n_before = len(pool)
        ctx = _ctx(s, org, user)
        out = await GraphProximityStage(gate=GraphGate(min_candidates=1)).run(
            "alpha beta gamma", ctx, pool
        )
        assert len(out) == n_before  # no new row: nb was already present
        nbc = next(c for c in out if c.blob_id == nb_blob)
        assert nbc.score > 0.3  # boosted
        assert "graph" in nbc.scores_by_stage
        assert nbc.provenance is None  # a live hit stays a live hit
        assert ctx.extras["graph_diag"]["contributed"] == 0


async def test_stage_gate_skips_thin_pool() -> None:
    """The gate skips (no traversal, list untouched, ``ran`` False) when the
    pool is below ``min_candidates`` or the query too short."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        seed = await _note(s, org, user, "seed", "alpha beta gamma")
        linked = await _note(s, org, user, "linked", "carbonara pancetta")
        await _link(s, org, seed, linked, "related")
    async with tenant_session(str(org), str(user)) as s:
        seed_blob = await _blob_of(s, org, seed)
        pool = [Candidate(blob_id=seed_blob, score=1.0)]  # 1 < default min_candidates
        ctx = _ctx(s, org, user)
        out = await GraphProximityStage().run("alpha beta gamma", ctx, pool)
        assert out is pool and len(out) == 1
        assert ctx.extras["graph_diag"] == {"ran": False, "contributed": 0}

        # Short query, ample pool -> still skips.
        pool2 = [Candidate(blob_id=seed_blob, score=1.0)] + [
            Candidate(blob_id=uuid.uuid4(), score=0.1) for _ in range(5)
        ]
        ctx2 = _ctx(s, org, user)
        out2 = await GraphProximityStage().run("hi", ctx2, pool2)
        assert len(out2) == len(pool2)
        assert ctx2.extras["graph_diag"]["ran"] is False


# ---------------------------------------------------------------- END-TO-END


async def _seed_pool(s: object, org: uuid.UUID, user: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Six 'quokka' notes (a pool >= the gate's min_candidates) plus a
    zero-overlap note LINKED to the strongest one. Returns (top, linked)."""
    top = await _note(s, org, user, "q0", "quokka biology field notes marsupial")
    for i in range(1, 6):
        await _note(s, org, user, f"q{i}", f"quokka habitat report number {i}")
    linked = await _note(s, org, user, "linked", "carbonara pancetta pecorino guanciale")
    await _link(s, org, top, linked, "hypha_of")
    return top, linked


async def test_off_is_byte_identical_and_meta_flags() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_pool(s, org, user)
    async with tenant_session(str(org), str(user)) as s:
        base = await memory.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            query="quokka biology field",
            operation_id="q-base",
        )
        off_hits, off_meta = await memory.retrieve_with_meta(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            query="quokka biology field",
            operation_id="q-off",
            graph=False,
        )
        # Default (graph off) == explicit graph=False, same ids AND scores.
        assert [h.blob.id for h in base] == [h.blob.id for h in off_hits]
        assert [round(h.rrf, 12) for h in base] == [round(h.rrf, 12) for h in off_hits]
        assert off_meta.graph_ran is False
        assert off_meta.graph_contributed == 0


async def test_on_runs_stage_and_reports_contribution(monkeypatch: pytest.MonkeyPatch) -> None:
    # Seed every matching note (not just the top-3) so the walk is robust to
    # the base ranking of the six near-identical 'quokka' notes.
    monkeypatch.setenv("MYCELIUM_GRAPH_STAGE_SEEDS", "12")
    get_settings.cache_clear()
    org, user = await _org()
    try:
        async with tenant_session(str(org), str(user)) as s:
            await _seed_pool(s, org, user)
        async with tenant_session(str(org), str(user)) as s:
            # Exclude the zero-overlap linked note from the dense branch so the
            # graph source is the ONLY way to surface it (real-corpus condition).
            await _set_semantic_floor(s, org, 0.05)
            _hits, meta = await memory.retrieve_with_meta(
                s,
                org_id=org,
                actor_id=user,
                project_id=None,
                query="quokka biology field",
                operation_id="q-on",
                graph=True,
            )
        # The stage ran and injected the linked note (the base branches never
        # find it -- zero token overlap). Survival to top-k is the ranking
        # A/B; here we pin the injection, which is floor-independent.
        assert meta.graph_ran is True
        assert meta.graph_contributed >= 1
    finally:
        get_settings.cache_clear()


async def test_run_eval_threads_graph_knob_non_decreasing() -> None:
    """``run_eval`` accepts the graph knob and recall never decreases with
    the source on (it only ADDS candidates). The strict lift is the
    real-corpus A/B, documented below."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        _top, linked = await _seed_pool(s, org, user)
    # Fresh session: the linked note's blob only exists once the deferred
    # per-part indexing of the previous transaction has committed.
    async with tenant_session(str(org), str(user)) as s:
        await _set_semantic_floor(s, org, 0.05)
        linked_blob = await _blob_of(s, org, linked)
        cases = [GoldCase(query="quokka biology field", expected=frozenset({linked_blob}))]
        off = await eval_offline.run_eval(
            s, org_id=org, actor_id=user, cases=cases, project_id=None, graph=False
        )
        on = await eval_offline.run_eval(
            s, org_id=org, actor_id=user, cases=cases, project_id=None, graph=True
        )
        assert on.recall_at_k >= off.recall_at_k


# Publishable A/B (real corpus, real embedder): run the public bench twice
#   uv run --with 'sentence-transformers>=3' python scripts/eval_public_bench.py \
#       --dataset locomo --path <locomo.json> --graph off
#   ... --graph on
# and report the recall_at_k delta (Fase 4 is the thesis's measured number).
