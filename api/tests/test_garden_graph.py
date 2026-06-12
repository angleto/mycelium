"""GET /garden/graph — note_edge_strength v1 + PageRank Phase 1
(tasks 4467acb4 + 8c0a8f08).

The integration tests build a few notes + manual links + shared
tags, hit the endpoint, and assert:

- soft-OR aggregation: a pair joined by both hypha_of AND a shared
  rare tag outweighs a pair joined by references only;
- canonical undirected edges: a single row per pair regardless of
  link direction;
- PageRank sums to 1 across the workspace; hubs (notes targeted by
  many links) rank higher than leaves;
- cross-tenant isolation: another workspace's links / centrality
  never bleed in.
"""

from __future__ import annotations

import uuid
from math import isclose

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient, ws: str = "G") -> dict[str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123", "workspace_name": ws},
        )
    ).json()
    return {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
    }


async def _make_note(c: AsyncClient, h: dict[str, str], title: str) -> str:
    r = await c.post("/notes", headers=h, json={"kind": "text", "title": title, "text": title})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _link(c: AsyncClient, h: dict[str, str], parent: str, child: str, kind: str) -> None:
    r = await c.post(
        f"/notes/{parent}/links",
        headers=h,
        json={"parent_note_id": parent, "child_note_id": child, "kind": kind},
    )
    assert r.status_code == 200, r.text


async def _tag(c: AsyncClient, h: dict[str, str], name: str) -> str:
    r = await c.post("/tags", headers=h, json={"kind": "generic", "name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _attach_tag(c: AsyncClient, h: dict[str, str], note_id: str, tag_id: str) -> None:
    r = await c.post(f"/notes/{note_id}/tags", headers=h, json={"tag_id": tag_id})
    assert r.status_code == 204, r.text


async def test_empty_workspace_returns_empty_graph() -> None:
    """No notes -> no edges, empty centrality. The endpoint must be
    safe to hit during onboarding before any content exists."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        r = await c.get("/garden/graph", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["edges"] == []
        assert body["centrality"] == {}


async def test_edge_weight_softor_orders_pairs_correctly() -> None:
    """A pair joined by hypha_of + a shared rare tag must outweigh a
    pair joined only by references. The soft-OR rule guarantees this
    monotonicity by construction."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        a = await _make_note(c, h, "A")
        b = await _make_note(c, h, "B")
        cnote = await _make_note(c, h, "C")
        d = await _make_note(c, h, "D")
        # Strong pair: hypha_of + a shared rare tag (only A and B carry it).
        rare = await _tag(c, h, "rare-marker")
        await _attach_tag(c, h, a, rare)
        await _attach_tag(c, h, b, rare)
        await _link(c, h, a, b, "hypha_of")
        # Weak pair: a single references link, no shared tag.
        await _link(c, h, cnote, d, "related")

        body = (await c.get("/garden/graph", headers=h)).json()
        weights = {tuple(sorted([e["src"], e["dst"]])): e["weight"] for e in body["edges"]}
        w_strong = weights[tuple(sorted([a, b]))]
        w_weak = weights[tuple(sorted([cnote, d]))]
        assert w_strong > w_weak, body


async def test_undirected_dedup_one_row_per_pair() -> None:
    """Two manual rows with reversed direction between the same pair
    (A->B as hypha_of and B->A as related) collapse to a single
    undirected edge whose weight is the soft-OR of both kinds."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        a = await _make_note(c, h, "A")
        b = await _make_note(c, h, "B")
        await _link(c, h, a, b, "hypha_of")
        await _link(c, h, b, a, "related")
        body = (await c.get("/garden/graph", headers=h)).json()
        # Exactly one row for the pair (canonical (src, dst) order).
        pairs = [tuple(sorted([e["src"], e["dst"]])) for e in body["edges"]]
        assert pairs.count(tuple(sorted([a, b]))) == 1
        # Weight = soft_or(0.85, 0.45) = 1 - 0.15 * 0.55 = 0.9175.
        w = next(
            e["weight"]
            for e in body["edges"]
            if tuple(sorted([e["src"], e["dst"]])) == tuple(sorted([a, b]))
        )
        assert isclose(w, 1 - 0.15 * 0.55, abs_tol=1e-6), w


async def test_pagerank_sums_to_one_and_hubs_outrank_leaves() -> None:
    """PageRank must be a distribution (sum ≈ 1.0) and a hub note that
    is the target of multiple links should rank above the leaves that
    only emit a single edge each."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        hub = await _make_note(c, h, "Hub")
        leaves = [await _make_note(c, h, f"Leaf {i}") for i in range(4)]
        for leaf in leaves:
            await _link(c, h, leaf, hub, "hypha_of")
        body = (await c.get("/garden/graph", headers=h)).json()
        ranks = body["centrality"]
        total = sum(ranks.values())
        assert isclose(total, 1.0, abs_tol=1e-3), total
        for leaf in leaves:
            assert ranks[hub] > ranks[leaf], (hub, leaf, ranks)


async def test_cross_tenant_isolation() -> None:
    """Workspace B never sees workspace A's edges or centrality even
    after both workspaces are populated with links. RLS on the
    underlying tables guarantees the boundary; this test pins the
    contract end-to-end through the endpoint."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h_a = await _signup(c, ws="A")
        h_b = await _signup(c, ws="B")
        a_a = await _make_note(c, h_a, "A1")
        a_b = await _make_note(c, h_a, "A2")
        await _link(c, h_a, a_a, a_b, "hypha_of")
        b_a = await _make_note(c, h_b, "B1")
        b_b = await _make_note(c, h_b, "B2")
        await _link(c, h_b, b_a, b_b, "related")

        body_b = (await c.get("/garden/graph", headers=h_b)).json()
        ids_in_b = {e["src"] for e in body_b["edges"]} | {e["dst"] for e in body_b["edges"]}
        assert a_a not in ids_in_b and a_b not in ids_in_b
        # And the centrality map is scoped to B only.
        assert a_a not in body_b["centrality"]


async def test_phase2_recency_live_and_betweenness_from_snapshot() -> None:
    """Phase 2 (task d8664631): ``recency`` is computed live (a fresh
    note reads ~1.0); ``betweenness`` is served from the materialised
    snapshot only — empty with a null ``analytics_computed_at`` before
    the worker's first refresh, populated after."""
    from flow_core.db import tenant_session
    from flow_core.services import graph_snapshot as snap_svc

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        a = await _make_note(c, h, "a")
        b = await _make_note(c, h, "b")
        n3 = await _make_note(c, h, "c")
        await _link(c, h, a, b, "related")
        await _link(c, h, b, n3, "related")

        before = (await c.get("/garden/graph", headers=h)).json()
        assert before["betweenness"] == {}
        assert before["analytics_computed_at"] is None
        # Fresh notes carry a full recency boost, separate from
        # centrality.
        assert before["recency"][a] >= 0.99

        # The worker tick's refresh (signature-gated) populates the
        # snapshot; the endpoint then serves the stored betweenness.
        org = h["X-Workspace-Id"]
        me = (await c.get("/auth/me", headers=h)).json()
        async with tenant_session(org, me["user_id"]) as s:
            assert await snap_svc.refresh_graph_snapshot(s, org_id=uuid.UUID(org)) is True

        after = (await c.get("/garden/graph", headers=h)).json()
        assert after["analytics_computed_at"] is not None
        # Path a-b-c: b is the bridge.
        assert isclose(after["betweenness"][b], 1.0, abs_tol=1e-6)
        assert after["betweenness"][a] == 0.0
