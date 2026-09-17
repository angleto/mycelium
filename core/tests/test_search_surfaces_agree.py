"""``search`` and ``memory_search`` must explain a hit the same way.

The two tools run the same retrieval pipeline and, for a blob both of
them reach, must publish the same ``scores_by_stage``. That agreement is
the whole reason the field was added to the unified surface: it had the
data and dropped it, so one tool could say WHY a row ranked and the
other could not.

Asserted rather than assumed, because the two serialise at separate
points in the same file and nothing else makes them agree. That is the
shape of drift this kind of test exists to catch: both sides keep
working, and they quietly stop saying the same thing.

What is deliberately NOT asserted here is the arithmetic. That is pinned
in ``test_retrieval_pipeline.py`` against the production weight table;
repeating it here would give two places to update and one of them would
be forgotten.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder

from mycelium_core.db import admin_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.services.auth import signup
from mycelium_mcp.server import _PRINCIPAL, memory_search, memory_write, search


@pytest.fixture()
def _fake_embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


async def _seed() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="SurfaceAgreement",
        )
    assert r.org_id is not None
    return r.org_id, r.user_id


async def test_both_surfaces_explain_the_same_blob_the_same_way(_fake_embedder: None) -> None:
    org, user = await _seed()
    reset = _PRINCIPAL.set((user, org, None))
    try:
        await memory_write(
            token="",
            org_id="",
            text="una misura del reranker sul nodo di produzione, con due cifre",
            operation_id="agree-w",
        )
        q = "misura del reranker sul nodo"
        mem = await memory_search(token="", org_id="", query=q, operation_id="agree-m", limit=10)
        uni = await search(
            token="", org_id="", q=q, operation_id="agree-s", kinds=["blob"], limit=10
        )
    finally:
        _PRINCIPAL.reset(reset)

    mem_by_blob = {h["blob"]["id"]: h.get("scores_by_stage", {}) for h in mem["hits"]}
    uni_by_blob = {h["blob_id"]: h.get("scores_by_stage", {}) for h in uni["hits"]}

    shared = set(mem_by_blob) & set(uni_by_blob)
    assert shared, (mem_by_blob, uni_by_blob)
    for blob_id in shared:
        assert mem_by_blob[blob_id] == uni_by_blob[blob_id], blob_id


async def test_the_unified_surface_does_not_return_an_empty_breakdown(
    _fake_embedder: None,
) -> None:
    """The regression the field was added for: the unified surface used to
    hand back a flat ``score`` and nothing else, so a caller could not tell
    a hit the lexical branch found from one only the dense branch reached
    and had to read the whole page to find out.

    Since 2026-09-17 the breakdown answers that question under
    ``explain=True`` instead of on every row of every search: the hits arrive
    ranked, so the ordinary reader already has the answer the floats encode,
    and they were measured at 43% of the tool's tokens across 83 recorded
    searches. The property under test is unchanged -- present means
    non-empty -- and the default is pinned below, because a breakdown that
    comes back by default is the cost this moved."""
    org, user = await _seed()
    reset = _PRINCIPAL.set((user, org, None))
    try:
        await memory_write(
            token="",
            org_id="",
            text="il collasso per nota non deve affamare la pagina",
            operation_id="agree-w2",
        )
        uni = await search(
            token="",
            org_id="",
            q="collasso per nota",
            operation_id="agree-s2",
            kinds=["blob"],
            limit=10,
            explain=True,
        )
        plain = await search(
            token="",
            org_id="",
            q="collasso per nota",
            operation_id="agree-s3",
            kinds=["blob"],
            limit=10,
        )
    finally:
        _PRINCIPAL.reset(reset)

    assert plain["hits"], plain
    assert all("score" not in h and "scores_by_stage" not in h for h in plain["hits"])
    assert uni["hits"], uni
    top = uni["hits"][0]
    assert top["scores_by_stage"], top
    # ``rrf`` is the fused score and equals the flat one, which is what
    # makes the branch entries readable as the reason for it.
    assert top["scores_by_stage"]["rrf"] == pytest.approx(top["score"])
    # And at least one branch entry, which is a 1-based rank and so is
    # never zero: an empty-but-present dict would pass a truthiness check
    # and mean nothing.
    branches = {k: v for k, v in top["scores_by_stage"].items() if k != "rrf"}
    assert branches, top
    assert all(v >= 1.0 for v in branches.values()), branches
