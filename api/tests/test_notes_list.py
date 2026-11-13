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
        lst = (await c.get("/notes", headers=h)).json()["items"]
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
            return {x["id"] for x in r.json()["items"]}

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


# ── one page at a time (ADR-0068) ───────────────────────────────────────────


async def _workspace(c: AsyncClient) -> dict[str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123", "workspace_name": "W"},
        )
    ).json()
    return {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}


async def _note(c: AsyncClient, h: dict[str, str], title: str) -> str:
    r = await c.post("/notes", headers=h, json={"kind": "text", "title": title, "text": title})
    assert r.status_code == 200, r.text
    return str(r.json()["id"])


async def test_the_cursor_walks_disjoint_pages_to_the_end() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _workspace(c)
        made = [await _note(c, h, f"n{i}") for i in range(5)]
        seen: list[str] = []
        cursor = None
        for _ in range(10):
            params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
            page = (await c.get("/notes", headers=h, params=params)).json()
            seen += [x["id"] for x in page["items"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert sorted(seen) == sorted(made)
        assert len(seen) == len(set(seen)) == 5
        assert seen == list(reversed(made)), "newest first"


async def test_the_bin_finds_a_note_deleted_long_before_the_newest_page() -> None:
    """The bin used to take the newest page of every state and keep the
    deleted rows, so an old deletion fell off it. Asked by state, it is the
    first row of its own page."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _workspace(c)
        old = await _note(c, h, "old")
        for i in range(3):
            await _note(c, h, f"newer {i}")
        cur = (await c.get(f"/notes/{old}", headers=h)).json()
        r = await c.post(
            f"/notes/{old}/delete", headers=h, json={"expected_version": cur["version"]}
        )
        assert r.status_code < 300, r.text
        everything = (
            await c.get("/notes", headers=h, params={"include_deleted": True, "limit": 3})
        ).json()
        assert old not in [x["id"] for x in everything["items"]], "the old shape would miss it"
        binned = (await c.get("/notes", headers=h, params={"only": "deleted", "limit": 3})).json()
        assert [x["id"] for x in binned["items"]] == [old]


async def test_notes_are_read_by_id_whatever_their_age() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _workspace(c)
        a = await _note(c, h, "a")
        b = await _note(c, h, "b")
        await _note(c, h, "c")
        page = (await c.get("/notes", headers=h, params={"ids": [a, b]})).json()
        assert sorted(x["id"] for x in page["items"]) == sorted([a, b])


async def test_a_malformed_cursor_is_a_coded_refusal() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _workspace(c)
        r = await c.get("/notes", headers=h, params={"cursor": "not-a-cursor"})
        assert r.status_code == 422
        assert "operation.invalid_argument" in r.text


async def test_a_focus_is_a_full_page_of_focused_notes() -> None:
    """The notes page used to filter its focus over the page it had loaded,
    so a focus whose notes were older than that page showed nothing. The
    server filters now: any of the focus's tags, before paging."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _workspace(c)
        # Creating a project is an owner's act, taken with "act as owner".
        owner = {**h, "X-Workspace-Role": "owner"}
        created = await c.post(
            "/projects", headers=owner, json={"name": f"P{uuid.uuid4().hex[:6]}"}
        )
        assert created.status_code == 200, created.text
        proj = created.json()
        old = await _note(c, h, "in focus, old")
        r = await c.post(f"/notes/{old}/tags", headers=h, json={"tag_id": proj["id"]})
        assert r.status_code < 300, r.text
        for i in range(3):
            await _note(c, h, f"out of focus {i}")
        page = (
            await c.get("/notes", headers=h, params={"any_tag_ids": [proj["id"]], "limit": 2})
        ).json()
        assert [x["id"] for x in page["items"]] == [old]
