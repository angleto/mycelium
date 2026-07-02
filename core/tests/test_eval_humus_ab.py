"""Humus A/B — HARNESS VALIDATION under a lexical oracle (task 4836a6cc).

WHAT THIS TEST IS (adversarial review 2026-07-02): a plumbing check that the
A/B/C matrix, the fairness filter and the per-config knobs compute what they
claim, on a controlled corpus where the expected outcome is KNOWN BY
CONSTRUCTION. It is NOT a measurement of humus's real-world value:

  * the FakeEmbedder is hashed bag-of-words -- cosine == token overlap. The
    real embedder (bge-m3) does paraphrase matching, so fairness verdicts
    obtained here (which case is a "genuine add") do NOT transfer to prod;
  * corpus, atoms, queries and the semantic floor were authored/tuned by the
    same hand; the consolidation query shares vocabulary with its atom by
    design. That makes the outcomes *construct validation*, not evidence.

What it legitimately establishes (mechanical facts, embedder-independent):
  * the monotonicity invariant: excluding humus atoms only frees slots, so
    raw recall(A_on) <= recall(B_branch_off) <= recall(C_atoms_excluded);
  * the cap arithmetic: budget = int(limit*0.3) -> 0 slots at limit<=3, so
    under current defaults NO humus atom can be served at k<=3 (pinned below);
  * knob semantics: default-preservation, Config C exclusion, per-kind branch
    attribution.

The real empirical gate runs on a real corpus + bge-m3 + independently sourced
queries + a pre-registered floor: scripts/eval_humus_ab.py.

Deterministic via the FakeEmbedder seam. Does NOT touch test_eval_offline's
committed gold baselines.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import NamedTuple

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.models.note import NoteKind
from mycelium_core.models.note_part_index_pointer import NotePartIndexPointer
from mycelium_core.models.organization import Organization
from mycelium_core.services import eval_offline, memory
from mycelium_core.services import notes as nt
from mycelium_core.services.auth import signup
from mycelium_core.services.eval_offline import ConsolidationCase, GoldCase

# Raw corpus. The first three are an INCIDENT CLUSTER: the same underlying
# cause (unbounded resource acquisition) described with DIFFERENT symptoms, and
# crucially none of them uses the generalization vocabulary of the pattern atom
# ("recurring throughline / outages"). The fourth is the self-contained source
# note Z that the single-source distillation compresses. The rest are
# distinct-topic filler so the corpus is not tiny and displacement has room.
_RAW: tuple[tuple[str, str], ...] = (
    (
        "checkout 503 mysql sockets",
        "Checkout returned many 503 errors on Tuesday; the mysql driver leaked "
        "sockets and saturated the pool.",
    ),
    (
        "billing cron worker handles",
        "The billing cron stalled overnight; a worker never released its handles, "
        "draining the pool.",
    ),
    (
        "sale traffic postgres sessions",
        "Sale traffic spiked latency; postgres sessions stayed open and exhausted the pool.",
    ),
    (
        "zephyr-ledger idempotency source",
        "Payments refactor lesson: the zephyr-ledger persists the idempotency key "
        "before the external call, so a retry never double-charges.",
    ),
    (
        "kubernetes autoscaler replicas",
        "Kubernetes horizontal pod autoscaler scales replicas on CPU load.",
    ),
    (
        "rust borrow checker lifetime",
        "The Rust borrow checker enforces lifetime and ownership at compile time.",
    ),
    (
        "espresso grind tamper crema",
        "Espresso extraction needs a fine grind and a firm tamper for good crema.",
    ),
    (
        "fourier transform frequencies",
        "The Fourier transform decomposes a signal into its constituent frequencies.",
    ),
    ("ski touring climbing skins", "Ski touring uses climbing skins on the base for the ascent."),
    ("sourdough hydration levain", "Sourdough needs a lively levain and high hydration to rise."),
)

# Humus atoms: (humus_kind, body). The consolidation queries live below.
_PATTERN_QUERY = "recurring throughline across the three outages prevention"
_DISTILL_QUERY = "zephyr-ledger idempotency key persist before external call"
_ATOMS: tuple[tuple[str, str], ...] = (
    (
        "pattern",  # multi-source generalization present in NO single raw note
        "Retrospective synthesis across three outages: the recurring throughline "
        "is unbounded resource acquisition without release. Prevention seed: "
        "enforce a ceiling and release in finally.",
    ),
    (
        "distillation",  # single-source compression of the zephyr-ledger source Z
        "Distilled lesson: persist the idempotency key before the external call in "
        "the zephyr-ledger, otherwise a retry double-charges.",
    ),
)

# A per-org semantic floor: the FakeEmbedder is bag-of-words, so common tokens
# ("the", "a") give every note a small non-zero cosine; with no floor the
# semantic branch returns the whole (tiny) corpus and the fairness probe cannot
# tell a genuine source match from noise. Genuine matches score ~0.29+ (several
# shared content tokens); a note sharing only "the" (weighted, since it recurs)
# reaches ~0.22, so 0.25 separates them.
#
# HONESTY NOTE (adversarial review 2026-07-02): this value was CALIBRATED
# against this corpus across runs (0.15 -> noise cleared the fairness probe;
# 0.30 -> cut genuine matches at cosine 0.294; 0.25 -> separates). The fairness
# verdict FLIPS with the floor (the pattern case is unfair at 0.15, fair at
# 0.25), which is why this fixture validates the harness, not humus value. On
# a real corpus the floor must be pre-registered from the measured cosine gap
# (scripts/diag_retrieval.py) BEFORE looking at outcomes, with a sweep
# reported (see run_humus_ab docstring).
_SEM_FLOOR = 0.25


@pytest.fixture
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


class _Seed(NamedTuple):
    org: uuid.UUID
    user: uuid.UUID
    raw_cases: list[GoldCase]
    consolidation_cases: list[ConsolidationCase]
    pattern_blob: uuid.UUID
    distill_blob: uuid.UUID


async def _seed() -> _Seed:
    """Seed a fresh org with the raw corpus + humus atoms; return the org/user
    plus the resolved gold cases and the atom blob ids."""
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="HUMUSAB")
    org, user = r.org_id, r.user_id
    raw_ids: list[uuid.UUID] = []
    atom_note_ids: dict[str, uuid.UUID] = {}
    async with tenant_session(str(org), str(user)) as s:
        # Set the semantic floor here (tenant_session, RLS-visible via the tenant
        # GUC; an admin_session UPDATE on Organization is silently RLS-filtered).
        # MERGE into the existing settings bag rather than replacing it, so any
        # signup-seeded defaults survive. See _SEM_FLOOR for the calibration note.
        bag = (
            await s.execute(select(Organization.settings).where(Organization.id == org))
        ).scalar_one_or_none()
        merged = dict(bag) if isinstance(bag, dict) else {}
        merged[memory.SEMANTIC_MIN_SIM_KEY] = _SEM_FLOOR
        await s.execute(update(Organization).where(Organization.id == org).values(settings=merged))
        for _q, body in _RAW:
            n = await nt.create_note(s, org_id=org, actor_id=user, kind=NoteKind.text, text=body)
            raw_ids.append(n.id)
        for kind, body in _ATOMS:
            n = await nt.create_note(s, org_id=org, actor_id=user, kind=NoteKind.text, text=body)
            # Hand-author the humus atom: the read path reads humus_flag/kind at
            # query time, so setting them here (rather than via the LLM producer)
            # isolates the RETRIEVAL branch from producer non-determinism.
            n.humus_flag = True
            n.humus_kind = kind
            atom_note_ids[kind] = n.id

    async def _blob_of(sess: AsyncSession, note_id: uuid.UUID) -> uuid.UUID:
        row = await sess.execute(
            select(NotePartIndexPointer.blob_id).where(NotePartIndexPointer.note_id == note_id)
        )
        return row.scalar_one()

    async with tenant_session(str(org), str(user)) as s:
        raw_blobs = [await _blob_of(s, nid) for nid in raw_ids]
        pattern_blob = await _blob_of(s, atom_note_ids["pattern"])
        distill_blob = await _blob_of(s, atom_note_ids["distillation"])
    raw_cases = [
        GoldCase(query=q, expected=frozenset({b}))
        for (q, _b), b in zip(_RAW, raw_blobs, strict=True)
    ]
    # Sources for the fairness filter: the pattern's sources are the three
    # incident notes (_RAW[0:3]); the distillation's source is the zephyr-ledger
    # note Z (_RAW[3]) it compresses.
    consolidation_cases = [
        ConsolidationCase(
            query=_PATTERN_QUERY,
            atom_expected=frozenset({pattern_blob}),
            source_expected=frozenset(raw_blobs[0:3]),
        ),
        ConsolidationCase(
            query=_DISTILL_QUERY,
            atom_expected=frozenset({distill_blob}),
            source_expected=frozenset({raw_blobs[3]}),
        ),
    ]
    return _Seed(
        org=org,
        user=user,
        raw_cases=raw_cases,
        consolidation_cases=consolidation_cases,
        pattern_blob=pattern_blob,
        distill_blob=distill_blob,
    )


async def test_humus_default_preserved(_embedder: None) -> None:
    """A call with no humus arg is identical to humus=True (the workspace
    default), i.e. the knob does not change the historical behaviour."""
    seed = await _seed()
    org, user = seed.org, seed.user
    async with tenant_session(str(org), str(user)) as s:
        default = await eval_offline.run_eval(
            s, org_id=org, actor_id=user, cases=seed.raw_cases, k=10
        )
        explicit = await eval_offline.run_eval(
            s, org_id=org, actor_id=user, cases=seed.raw_cases, k=10, humus=True
        )
    assert default.recall_at_k == explicit.recall_at_k
    assert default.mrr == explicit.mrr


async def test_humus_ab_matrix(_embedder: None) -> None:
    seed = await _seed()
    org, user = seed.org, seed.user
    async with tenant_session(str(org), str(user)) as s:
        report = await eval_offline.run_humus_ab(
            s,
            org_id=org,
            actor_id=user,
            raw_cases=seed.raw_cases,
            consolidation_cases=seed.consolidation_cases,
            ks=(3, 5, 10),
        )
    print("\n" + report.render())  # visible under `pytest -s`

    # Fairness filter, BY CONSTRUCTION under the lexical oracle: the pattern
    # query shares no content token with the incident sources (kept), the
    # distillation query shares tokens with source Z (dropped). This validates
    # the filter's mechanics; it says nothing about real corpora, where a
    # paraphrase embedder can retrieve sources without token overlap.
    assert _PATTERN_QUERY in report.fair_consolidation
    assert _DISTILL_QUERY in report.dropped_unfair

    kmax = max(report.ks)
    a_con = report.cell("A_on", "consolidation", kmax)
    c_con = report.cell("C_atoms_excluded", "consolidation", kmax)
    assert a_con is not None and c_con is not None
    # The planted add is detected at kmax: with atoms present the atom is
    # retrieved; with atoms excluded it is unreachable (fairness -> recall 0).
    assert a_con.recall_at_k == 1.0
    assert c_con.recall_at_k == 0.0

    # CAP-DEFECT PIN (adversarial review 2026-07-02): budget = int(k*0.3) = 0
    # at k<=3, so under current defaults the cap deletes EVERY humus candidate
    # at small k -- including a genuinely best-matching consolidation atom --
    # while the no-machinery config (B) serves it. This assertion pins the
    # defect so the N3 cap redesign (ceil / min-1 / marginal-only) must
    # consciously update it. If this fails, the cap behavior changed.
    a_con3 = report.cell("A_on", "consolidation", 3)
    b_con3 = report.cell("B_branch_off", "consolidation", 3)
    assert a_con3 is not None and b_con3 is not None
    assert a_con3.recall_at_k == 0.0  # machinery blocks the atom at k=3
    assert b_con3.recall_at_k == 1.0  # atoms-as-notes serve it fine

    # DISPLACEMENT guards, per-k. Monotonicity is structural (excluding atoms
    # can only free slots); on THIS corpus the stronger equality also holds:
    # humus costs no raw RECALL at any k (the only displacement effect is a
    # single MRR rank swap at k>=5). The equality is a real regression guard:
    # if a future change makes the humus branch push a raw hit out of top-k,
    # this fails loudly. (An earlier run violated c_raw==1.0 at k=3 -- that
    # was the 0.30 floor cutting a genuine cosine-0.294 match, fixed by the
    # 0.25 calibration, not a humus effect.)
    for k in report.ks:
        a_raw = report.cell("A_on", "raw", k)
        b_raw = report.cell("B_branch_off", "raw", k)
        c_raw = report.cell("C_atoms_excluded", "raw", k)
        assert a_raw is not None and b_raw is not None and c_raw is not None
        assert a_raw.recall_at_k <= b_raw.recall_at_k <= c_raw.recall_at_k
        assert c_raw.recall_at_k == 1.0  # humus-free baseline: every note findable
        assert a_raw.recall_at_k == 1.0  # and humus displaces no raw hit (recall)


async def test_humus_kinds_restricts_branch(_embedder: None) -> None:
    """``humus_kinds`` restricts the humus BRANCH: the pattern atom is stamped
    provenance='humus' only when 'pattern' is in the kinds (else it is still
    retrievable via the base branches, but unmarked)."""
    seed = await _seed()
    org, user, pattern_blob = seed.org, seed.user, seed.pattern_blob
    async with tenant_session(str(org), str(user)) as s:
        hits_pat, _ = await memory.retrieve_with_meta(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            query=_PATTERN_QUERY,
            operation_id=f"eval-{uuid.uuid4().hex}",
            limit=10,
            humus_kinds=frozenset({"pattern"}),
        )
        hits_dis, _ = await memory.retrieve_with_meta(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            query=_PATTERN_QUERY,
            operation_id=f"eval-{uuid.uuid4().hex}",
            limit=10,
            humus_kinds=frozenset({"distillation"}),
        )
    pat_hit = next((h for h in hits_pat if h.blob.id == pattern_blob), None)
    dis_hit = next((h for h in hits_dis if h.blob.id == pattern_blob), None)
    assert pat_hit is not None and pat_hit.provenance == "humus"
    # Branch restricted to 'distillation' -> the pattern atom is NOT in the humus
    # branch; it still surfaces via base retrieval, but without the humus marker.
    assert dis_hit is not None and dis_hit.provenance is None


async def test_run_eval_project_perimeter(_embedder: None) -> None:
    """The project_id bug + fix (adversarial review 2026-07-02, MAJOR):
    ``_project_pred(None)`` means ``project_id IS NULL``, NOT "no filter". A
    project-scoped corpus evaluated without passing the project measures
    recall 0 artificially; passing ``project_id`` restores the perimeter.
    This is the exact failure mode the real-corpus runner had in v1."""
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="EVALPROJ")
    org, user = r.org_id, r.user_id
    proj = uuid.uuid4()
    async with tenant_session(str(org), str(user)) as s:
        blob = await memory.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,  # project-scoped, like a real note-blob corpus
            text_body="Kubernetes horizontal pod autoscaler scales replicas on CPU load",
            operation_id="seed-proj",
        )
        case = GoldCase(query="kubernetes autoscaler replicas", expected=frozenset({blob.id}))
        # Without project_id (the v1 bug): perimeter = blobs with NO project ->
        # the case silently misses.
        missed = await eval_offline.run_eval(s, org_id=org, actor_id=user, cases=[case], k=5)
        assert missed.recall_at_k == 0.0
        # With the project perimeter: the same case is found.
        found = await eval_offline.run_eval(
            s, org_id=org, actor_id=user, cases=[case], k=5, project_id=proj
        )
        assert found.recall_at_k == 1.0
