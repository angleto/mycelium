"""@note support: notes list endpoint + Apple-Notes auto-title (first
line becomes the title when none is given)."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_notes_list_and_auto_title() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        h = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}

        # No title -> first non-empty line becomes the title.
        n1 = (
            await c.post(
                "/notes",
                headers=h,
                json={"kind": "text", "text": "Refactor scheduler\nsplit CPM core"},
            )
        ).json()
        assert n1["title"] == "Refactor scheduler"

        # Explicit title is kept.
        n2 = (
            await c.post(
                "/notes",
                headers=h,
                json={"kind": "text", "title": "Keep me", "text": "body line"},
            )
        ).json()
        assert n2["title"] == "Keep me"

        # List endpoint returns them (newest first).
        lst = (await c.get("/notes", headers=h)).json()
        ids = [x["id"] for x in lst]
        assert n1["id"] in ids
        assert n2["id"] in ids
        by_id = {x["id"]: x for x in lst}
        assert by_id[n1["id"]]["title"] == "Refactor scheduler"


async def test_notes_list_q_search() -> None:
    """``GET /notes?q=`` filters server-side over the WHOLE corpus: title,
    part body, and tag name. Multiple terms are ANDed, fields ORed, and
    matching is case-insensitive."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        h = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}

        # Unique marker so the query cannot collide with rows other tests
        # leave in the shared dev database.
        tok = uuid.uuid4().hex[:8]

        async def mk(title: str, text: str) -> str:
            r = await c.post(
                "/notes",
                headers=h,
                json={"kind": "text", "title": title, "text": text},
            )
            return r.json()["id"]

        # Marker in the TITLE / in the part BODY / only via a TAG name /
        # nowhere.
        n_title = await mk(f"Zephyr {tok} plan", "alpha body")
        n_body = await mk("Unrelated heading", f"deep {tok} note beta")
        n_tag = await mk("Tagged only", "no marker in the body")
        n_none = await mk("Nothing here", "plain content")

        tag = (
            await c.post(
                "/tags",
                headers=h,
                json={"name": f"flowtag-{tok}", "kind": "generic"},
            )
        ).json()
        r_attach = await c.post(f"/notes/{n_tag}/tags", headers=h, json={"tag_id": tag["id"]})
        assert r_attach.status_code == 204

        async def search(q: str) -> set[str]:
            r = await c.get("/notes", headers=h, params={"q": q})
            return {x["id"] for x in r.json()}

        # One marker term reaches title, body AND tag-name notes, never
        # the unrelated one.
        ids = await search(tok)
        assert {n_title, n_body, n_tag} <= ids
        assert n_none not in ids

        # Case-insensitive.
        assert {n_title, n_body, n_tag} <= await search(tok.upper())

        # Two terms are ANDed: ``beta`` occurs only in the body note, so
        # ``<tok> beta`` narrows to it alone.
        ids_and = await search(f"{tok} beta")
        assert n_body in ids_and
        assert n_title not in ids_and
        assert n_tag not in ids_and

        # Empty q is a no-op (the normal list, not zero rows).
        assert n_none in await search("")
