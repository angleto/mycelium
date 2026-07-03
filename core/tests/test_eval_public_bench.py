"""Public-benchmark adapters (LongMemEval / LOCOMO -> run_eval), task cc4653bd.

Synthetic fixtures shaped EXACTLY like the published datasets (field names
verified 2026-07-03 against the source repos -- see the module docstring of
``eval_public_bench``) so the parsers and the ingest->resolve->score loop run
deterministically in CI under the FakeEmbedder. The REAL datasets are
operator-provided and never committed: the smoke test over them is gated on
``MYCELIUM_BENCH_DATA`` (off-CI marker) and only exercises parser robustness,
not retrieval quality.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from _fake_embedder import FakeEmbedder

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.services import eval_public_bench as bench
from mycelium_core.services.auth import signup

# --- Synthetic fixtures (dataset-shaped; lexically distinct like the CI
# gold set so ranks are unambiguous under the FakeEmbedder). ---

LONGMEMEVAL_SYNTHETIC: list[dict[str, object]] = [
    {
        "question_id": "syn-001",
        "question_type": "multi-session",
        "question": "kubernetes autoscaler replicas",
        "answer": "It scales replicas on CPU load.",
        "question_date": "2023/06/01 (Thu) 10:00",
        "haystack_session_ids": ["s-a", "s-b"],
        "haystack_dates": ["2023/05/20 (Sat) 02:21", "2023/05/25 (Thu) 16:44"],
        "haystack_sessions": [
            [
                {
                    "role": "user",
                    "content": ("Kubernetes horizontal pod autoscaler scales replicas on CPU load"),
                    "has_answer": True,
                },
                {"role": "assistant", "content": "Noted."},
            ],
            [
                {
                    "role": "user",
                    "content": "Espresso extraction needs a fine grind and a firm tamper",
                },
            ],
        ],
        "answer_session_ids": ["s-a"],
    },
    {
        "question_id": "syn-002_abs",
        "question_type": "single-session-user",
        "question": "quantum chromodynamics lattice coupling",
        "answer": "N/A",
        "question_date": "2023/06/02 (Fri) 09:00",
        "haystack_session_ids": ["s-c"],
        "haystack_dates": ["2023/05/26 (Fri) 11:02"],
        "haystack_sessions": [
            [{"role": "user", "content": "Ski touring uses climbing skins for the ascent"}],
        ],
        "answer_session_ids": [],
    },
]

LOCOMO_SYNTHETIC: dict[str, object] = {
    "sample_id": "conv-syn",
    "conversation": {
        "speaker_a": "Caroline",
        "speaker_b": "Melanie",
        "session_1_date_time": "1:56 pm on 8 May, 2023",
        "session_1": [
            {
                "speaker": "Caroline",
                "dia_id": "D1:1",
                "text": "The Rust borrow checker enforces lifetime and ownership",
            },
            {
                "speaker": "Melanie",
                "dia_id": "D1:2",
                "text": "I went hiking",
                "blip_caption": "a mountain trail at sunrise",
            },
        ],
        "session_2_date_time": "10:03 am on 12 May, 2023",
        "session_2": [
            {
                "speaker": "Caroline",
                "dia_id": "D2:1",
                "text": "The Fourier transform decomposes a signal into frequencies",
            },
        ],
    },
    "qa": [
        {
            "question": "rust borrow checker lifetime",
            "answer": "It enforces lifetime and ownership.",
            "evidence": ["D1:1"],
            "category": 4,
        },
        {
            "question": "fourier transform frequencies",
            "answer": "Into constituent frequencies.",
            "evidence": ["D2:1"],
            "category": 2,
        },
        {
            "question": "underwater basket weaving championship",
            "adversarial_answer": "Not mentioned.",
            "category": 5,
        },
    ],
}


@pytest.fixture
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def test_parse_longmemeval_instance_shape() -> None:
    inst = bench.parse_longmemeval_instance(LONGMEMEVAL_SYNTHETIC[0])
    assert inst.instance_id == "syn-001"
    assert [u.source_id for u in inst.units] == ["s-a", "s-b"]
    # Date header + role-attributed turns end up in the stored text.
    assert inst.units[0].text.startswith("[session date: 2023/05/20")
    assert "user: Kubernetes horizontal" in inst.units[0].text
    (q,) = inst.questions
    assert q.category == "multi-session"
    assert not q.abstention
    assert q.evidence_source_ids == frozenset({"s-a"})


def test_parse_longmemeval_abstention_and_errors() -> None:
    inst = bench.parse_longmemeval_instance(LONGMEMEVAL_SYNTHETIC[1])
    (q,) = inst.questions
    assert q.abstention  # _abs suffix
    assert q.evidence_source_ids == frozenset()
    broken = dict(LONGMEMEVAL_SYNTHETIC[0])
    broken["haystack_dates"] = ["only-one"]
    with pytest.raises(ValueError, match="lengths differ"):
        bench.parse_longmemeval_instance(broken)
    no_evidence = dict(LONGMEMEVAL_SYNTHETIC[0])
    no_evidence["answer_session_ids"] = []
    with pytest.raises(ValueError, match="without answer_session_ids"):
        bench.parse_longmemeval_instance(no_evidence)


def test_parse_locomo_sample_shape() -> None:
    inst = bench.parse_locomo_sample(LOCOMO_SYNTHETIC)
    assert inst.instance_id == "conv-syn"
    assert [u.source_id for u in inst.units] == ["D1:1", "D1:2", "D2:1"]
    # Speaker + date header; BLIP caption folded into the turn text.
    assert inst.units[0].text.startswith("[1:56 pm on 8 May, 2023] Caroline:")
    assert "shares a photo: a mountain trail" in inst.units[1].text
    cats = [q.category for q in inst.questions]
    assert cats == ["single-hop", "temporal", "adversarial"]
    assert inst.questions[2].abstention  # category 5
    assert inst.questions[0].evidence_source_ids == frozenset({"D1:1"})


async def _seed_org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="BENCH")
    return r.org_id, r.user_id


async def test_ingest_and_score_longmemeval(_embedder: None) -> None:
    """End-to-end on the synthetic instance: the evidence session is the top
    hit, the distractor is not required, tokens are counted."""
    inst = bench.parse_longmemeval_instance(LONGMEMEVAL_SYNTHETIC[0])
    org, user = await _seed_org()
    async with tenant_session(str(org), str(user)) as s:
        await bench.ingest_instance(s, org_id=org, actor_id=user, instance=inst)
    async with tenant_session(str(org), str(user)) as s:
        resolved = await bench.resolve_evidence(s, org_id=org, instance=inst)
        assert set(resolved) == {"s-a", "s-b"}
        score = await bench.score_instance(s, org_id=org, actor_id=user, instance=inst, k=5)
    (res,) = score.results
    assert res.rank == 1
    assert res.served_tokens > 0
    assert not score.skipped_no_evidence


async def test_ingest_and_score_locomo_with_abstention(_embedder: None) -> None:
    inst = bench.parse_locomo_sample(LOCOMO_SYNTHETIC)
    org, user = await _seed_org()
    async with tenant_session(str(org), str(user)) as s:
        await bench.ingest_instance(s, org_id=org, actor_id=user, instance=inst)
    async with tenant_session(str(org), str(user)) as s:
        score = await bench.score_instance(s, org_id=org, actor_id=user, instance=inst, k=5)
        models = await bench.corpus_embedder_models(s, org_id=org)
    by_qid = {r.qid: r for r in score.results}
    assert by_qid["conv-syn:0"].rank == 1
    assert by_qid["conv-syn:1"].rank == 1
    abst = by_qid["conv-syn:2"]
    assert abst.abstention
    # With no grader floor the pipeline still serves k hits, so the honest
    # abstention baseline is False here (the f0d24fdb lever, not a bug).
    assert abst.abstain_correct is False
    report = bench.aggregate("locomo", 5, [score], models)
    assert report.n_scored == 2 and report.n_abstention == 1
    assert report.recall_at_k == 1.0
    assert report.abstention_correct_rate == 0.0
    assert {c.category for c in report.per_category} == {
        "single-hop",
        "temporal",
        "adversarial (abstain)",
    }
    assert report.tokens_per_query > 0
    assert report.embedder_models  # honesty label always present


async def test_skipped_unresolvable_evidence_is_reported(_embedder: None) -> None:
    """A question whose evidence ids resolve to no stored blob must be
    reported as skipped, never scored as an artificial miss."""
    inst = bench.parse_locomo_sample(LOCOMO_SYNTHETIC)
    org, user = await _seed_org()
    async with tenant_session(str(org), str(user)) as s:
        await bench.ingest_instance(s, org_id=org, actor_id=user, instance=inst)
    ghost = bench.BenchInstance(
        instance_id=inst.instance_id,
        units=inst.units,
        questions=(
            bench.BenchQuestion(
                qid="ghost",
                query="anything",
                answer="",
                category="single-hop",
                evidence_source_ids=frozenset({"D9:9"}),
                abstention=False,
            ),
        ),
    )
    async with tenant_session(str(org), str(user)) as s:
        score = await bench.score_instance(s, org_id=org, actor_id=user, instance=ghost, k=5)
    assert score.skipped_no_evidence == ("ghost",)
    assert not score.results


# --- Off-CI: real operator-provided datasets (task cc4653bd: ~100MB, never
# committed). Set MYCELIUM_BENCH_DATA to the datasets directory to run. ---

_BENCH_DATA = os.environ.get("MYCELIUM_BENCH_DATA", "")


@pytest.mark.skipif(not _BENCH_DATA, reason="MYCELIUM_BENCH_DATA not set (real datasets)")
def test_real_datasets_parse() -> None:
    root = Path(_BENCH_DATA)
    parsed_any = False
    for name, parse in (
        ("longmemeval_oracle.json", bench.parse_longmemeval_instance),
        ("locomo10.json", bench.parse_locomo_sample),
    ):
        path = root / name
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, list) and data
        for obj in data:
            inst = parse(obj)
            assert inst.units and inst.questions
        parsed_any = True
    assert parsed_any, f"no known dataset file found under {root}"
