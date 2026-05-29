"""GET /garden/walk — focused (PPR seeded) + free_wander (Node2Vec).

Task 5bf31b63. The endpoint returns the top-K nodes by induced mass
in focused mode and the explicit trajectory in free_wander mode. The
tests build a small ring + hub graph and assert basic structural
invariants:

- focused walk from a hub returns its neighbours weighted by PPR;
- a non-existent seed yields a zero-distribution focused walk;
- free_wander walks are deterministic when seeded with ``seed_rng``;
- cross-tenant isolation (another workspace's walk is invisible).
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient, ws: str = "W") -> dict[str, str]:
    r = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123", "workspace_name": ws},
        )
    ).json()
    return {
        "Authorization": f"Bearer {r['token']}",
        "X-Workspace-Id": r["workspace_id"],
    }


async def _note(c: AsyncClient, h: dict[str, str], title: str) -> str:
    r = await c.post("/notes", headers=h, json={"kind": "text", "title": title, "text": title})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _link(c: AsyncClient, h: dict[str, str], parent: str, child: str) -> None:
    r = await c.post(
        f"/notes/{parent}/links",
        headers=h,
        json={
            "parent_note_id": parent,
            "child_note_id": child,
            "kind": "related",
        },
    )
    assert r.status_code == 200, r.text


async def test_focused_walk_ranks_hub_neighbours() -> None:
    """A 5-leaf star: focused walk seeded on the hub returns each
    leaf with non-zero mass, ranked by step index. The hub itself
    is excluded from the output (the caller already has it)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        hub = await _note(c, h, "hub")
        leaves = [await _note(c, h, f"leaf-{i}") for i in range(5)]
        for leaf in leaves:
            await _link(c, h, hub, leaf)
        r = await c.get(
            "/garden/walk", headers=h, params={"seed": hub, "mode": "focused", "budget": 10}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["seed"] == hub
        assert body["mode"] == "focused"
        ids_in_walk = {s["note_id"] for s in body["steps"]}
        # Every leaf must appear (PPR mass leaks to them via damping).
        assert ids_in_walk >= set(leaves)
        # Hub is the seed and must NOT appear in steps.
        assert hub not in ids_in_walk
        # Steps are 1-indexed and dense.
        steps = [s["step"] for s in body["steps"]]
        assert steps == list(range(1, len(steps) + 1))


async def test_free_wander_walk_is_reproducible() -> None:
    """``seed_rng`` pins the RNG: two calls with the same seed return
    the same trajectory (modulo trailing termination ties)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        a = await _note(c, h, "a")
        b = await _note(c, h, "b")
        cnode = await _note(c, h, "c")
        d = await _note(c, h, "d")
        # 4-node ring
        await _link(c, h, a, b)
        await _link(c, h, b, cnode)
        await _link(c, h, cnode, d)
        await _link(c, h, d, a)
        params = {"seed": a, "mode": "free_wander", "budget": 6, "seed_rng": 17}
        r1 = (await c.get("/garden/walk", headers=h, params=params)).json()
        r2 = (await c.get("/garden/walk", headers=h, params=params)).json()
        assert r1["mode"] == "free_wander"
        ids1 = [s["note_id"] for s in r1["steps"]]
        ids2 = [s["note_id"] for s in r2["steps"]]
        assert ids1 == ids2
        # Walk starts at the seed.
        assert ids1[0] == a


async def test_walk_across_tenants_is_isolated() -> None:
    """A note id from another workspace must not be walkable from
    ours. The seed has nothing to walk to, so the response is empty."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h1 = await _signup(c, ws="W1")
        h2 = await _signup(c, ws="W2")
        n1 = await _note(c, h1, "x")
        # Use a random uuid as the seed in tenant 2 (n1 is invisible).
        r = await c.get(
            "/garden/walk",
            headers=h2,
            params={"seed": n1, "mode": "focused", "budget": 5},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # The seed has no presence in W2, so the focused walk has
        # no mass to distribute -> empty steps.
        assert body["steps"] == [] or all(s["weight"] == 0 for s in body["steps"])
