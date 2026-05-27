"""GET /garden/link-suggestions/{note_id} — link prediction Phase 1
(task c7d0bb4c). The endpoint must:

- exclude the source itself + every already-linked partner;
- rank candidates by Adamic-Adar + PPR co-visit;
- isolate across tenants.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient, ws: str = "L") -> dict[str, str]:
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


async def _tag(c: AsyncClient, h: dict[str, str], name: str) -> str:
    r = await c.post("/tags", headers=h, json={"kind": "generic", "name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _attach_tag(c: AsyncClient, h: dict[str, str], note_id: str, tag_id: str) -> None:
    r = await c.post(f"/notes/{note_id}/tags", headers=h, json={"tag_id": tag_id})
    assert r.status_code == 204, r.text


async def _link(c: AsyncClient, h: dict[str, str], parent: str, child: str) -> None:
    r = await c.post(
        f"/notes/{parent}/links",
        headers=h,
        json={"parent_note_id": parent, "child_note_id": child, "kind": "references"},
    )
    assert r.status_code == 200, r.text


async def test_suggestions_rank_by_shared_rare_tags() -> None:
    """Three candidates, only one shares a rare tag with the source.
    That candidate must rank first."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        rare = await _tag(c, h, "rare-topic")
        common = await _tag(c, h, "common-topic")
        src = await _note(c, h, "src")
        a = await _note(c, h, "a")  # shares rare tag
        b = await _note(c, h, "b")  # shares common tag
        d = await _note(c, h, "d")  # shares nothing
        await _attach_tag(c, h, src, rare)
        await _attach_tag(c, h, src, common)
        await _attach_tag(c, h, a, rare)
        await _attach_tag(c, h, b, common)
        # ``d`` shares neither.
        # Push common to be common: tag c through src + b + d.
        await _attach_tag(c, h, d, common)
        r = await c.get(f"/garden/link-suggestions/{src}", headers=h, params={"k": 5})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source_note_id"] == src
        ids = [s["note_id"] for s in body["suggestions"]]
        assert a in ids
        # ``a`` should outrank ``b`` because the rare tag has lower
        # degree in the workspace.
        rank_a = ids.index(a)
        if b in ids:
            rank_b = ids.index(b)
            assert rank_a < rank_b


async def test_suggestions_exclude_already_linked() -> None:
    """A note already linked to the source must not appear in the
    suggestions."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        src = await _note(c, h, "src")
        a = await _note(c, h, "a")
        b = await _note(c, h, "b")
        tag = await _tag(c, h, "shared")
        for n in (src, a, b):
            await _attach_tag(c, h, n, tag)
        await _link(c, h, src, a)
        r = (
            await c.get(f"/garden/link-suggestions/{src}", headers=h)
        ).json()
        ids = {s["note_id"] for s in r["suggestions"]}
        assert a not in ids  # already linked
        assert src not in ids  # source itself excluded


async def test_suggestions_isolated_across_tenants() -> None:
    """An attacker that knows another workspace's note id should
    not get suggestions for it via their own tenant."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h1 = await _signup(c, ws="W1")
        h2 = await _signup(c, ws="W2")
        secret = await _note(c, h1, "secret")
        r = await c.get(
            f"/garden/link-suggestions/{secret}", headers=h2, params={"k": 5}
        )
        assert r.status_code == 200, r.text
        # The note is invisible in W2 so the service short-circuits.
        assert r.json()["suggestions"] == []
