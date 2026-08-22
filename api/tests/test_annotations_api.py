"""Inline annotations (comments + suggestions) on markdown documents.

Integration coverage over the full stack (router -> service -> DB,
under RLS):

- a task comment is an annotation on ``task_description``; the legacy
  ``/tasks/{id}/comments`` endpoint still works and returns the new
  shape; whole-document comments are the work diary;
- an anchored comment on a note part carries its quote and an Identity
  author;
- a suggestion on a note part splices ``original -> proposed`` into the
  body on accept, and goes "stale" (no change) when the target text is
  gone;
- resolve / reopen / edit / soft-delete lifecycle;
- replies inherit the parent's document;
- cross-org isolation: a foreign document id is rejected;
- accept/reject only apply to a suggestion.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> dict[str, str]:
    a = (await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})).json()
    return {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}


async def _note_with_part(c: AsyncClient, h: dict[str, str], body: str) -> tuple[str, str]:
    """Create a text note and return ``(note_id, part0_id)``."""
    note = (await c.post("/notes", headers=h, json={"kind": "text", "text": body})).json()
    full = (await c.get(f"/notes/{note['id']}", headers=h)).json()
    return note["id"], full["parts"][0]["id"]


async def test_task_comment_is_annotation_and_legacy_endpoint_works() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        tid = (await c.post("/tasks", headers=h, json={"title": "Ship it"})).json()["id"]

        # legacy task-comment endpoint still works, now returns AnnotationOut
        r = await c.post(f"/tasks/{tid}/comments", headers=h, json={"body": "started"})
        assert r.status_code == 200, r.text
        a = r.json()
        assert a["doc_kind"] == "task_description"
        assert a["doc_id"] == tid
        assert a["kind"] == "comment"
        assert a["status"] == "open"
        assert a["author_identity_id"] is not None  # human author recorded via Identity
        # Resolved author (task 515e13fb): a human name, not a raw id prefix.
        assert a["author_kind"] == "user"
        assert a["author_handle"]  # a real handle
        assert a["author_label"] is None  # label is the ai_assistant display name only

        # a second diary entry via the generic endpoint
        await c.post(
            "/annotations/comment",
            headers=h,
            json={"doc_kind": "task_description", "doc_id": tid, "body": "fixed the flake"},
        )
        rows = (await c.get(f"/tasks/{tid}/comments", headers=h)).json()
        assert [x["body"] for x in rows] == ["started", "fixed the flake"]  # chronological diary

        # also visible through the generic listing
        rows2 = (
            await c.get(
                "/annotations", headers=h, params={"doc_kind": "task_description", "doc_id": tid}
            )
        ).json()
        assert len(rows2) == 2


async def test_note_part_anchored_comment_and_reply() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        _nid, pid = await _note_with_part(c, h, "The quick brown fox jumps.")

        r = await c.post(
            "/annotations/comment",
            headers=h,
            json={
                "doc_kind": "note_part",
                "doc_id": pid,
                "body": "vivid",
                "anchor_quote": "brown fox",
            },
        )
        assert r.status_code == 200, r.text
        parent = r.json()
        assert parent["anchor_quote"] == "brown fox"
        assert parent["doc_kind"] == "note_part"

        # reply inherits the document from the parent
        reply = (
            await c.post(
                "/annotations/comment",
                headers=h,
                json={
                    "doc_kind": "note_part",
                    "doc_id": pid,
                    "body": "agreed",
                    "parent_id": parent["id"],
                },
            )
        ).json()
        assert reply["parent_id"] == parent["id"]
        assert reply["doc_id"] == pid


async def test_suggestion_accept_splices_body() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        nid, pid = await _note_with_part(c, h, "The quick brown fox jumps.")

        sug = (
            await c.post(
                "/annotations/suggestion",
                headers=h,
                json={
                    "doc_kind": "note_part",
                    "doc_id": pid,
                    "original_text": "quick brown fox",
                    "proposed_text": "lazy dog",
                    "rationale": "shorter",
                },
            )
        ).json()
        assert sug["kind"] == "suggestion"
        assert sug["anchor_quote"] == "quick brown fox"  # struck text is the anchor

        # before accept the body is untouched
        before = (await c.get(f"/notes/{nid}", headers=h)).json()["parts"][0]["body"]
        assert before == "The quick brown fox jumps."

        acc = await c.post(
            f"/annotations/{sug['id']}/accept", headers=h, json={"expected_version": sug["version"]}
        )
        assert acc.status_code == 200, acc.text

        after = (await c.get(f"/notes/{nid}", headers=h)).json()["parts"][0]["body"]
        assert after == "The lazy dog jumps."  # spliced in
        assert (await c.get(f"/annotations/{sug['id']}", headers=h)).json()["status"] == "accepted"


async def test_suggestion_goes_stale_when_target_gone() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        nid, pid = await _note_with_part(c, h, "Alpha beta gamma.")
        sug = (
            await c.post(
                "/annotations/suggestion",
                headers=h,
                json={
                    "doc_kind": "note_part",
                    "doc_id": pid,
                    "original_text": "not present anywhere",
                    "proposed_text": "x",
                },
            )
        ).json()
        r = await c.post(
            f"/annotations/{sug['id']}/accept", headers=h, json={"expected_version": sug["version"]}
        )
        assert r.status_code >= 400  # SUGGESTION_STALE, body unchanged
        body = (await c.get(f"/notes/{nid}", headers=h)).json()["parts"][0]["body"]
        assert body == "Alpha beta gamma."


async def _accept_suggestion(
    c: AsyncClient,
    h: dict[str, str],
    pid: str,
    original: str,
    proposed: str,
    *,
    anchor_domain: str | None = None,
):
    payload: dict[str, object] = {
        "doc_kind": "note_part",
        "doc_id": pid,
        "original_text": original,
        "proposed_text": proposed,
    }
    if anchor_domain is not None:
        payload["anchor_domain"] = anchor_domain
    sug = (await c.post("/annotations/suggestion", headers=h, json=payload)).json()
    return await c.post(
        f"/annotations/{sug['id']}/accept", headers=h, json={"expected_version": sug["version"]}
    )


async def test_accept_splices_markdown_source() -> None:
    """End-to-end through the real stack: a suggestion's ``original_text`` is
    markdown SOURCE, and accepting replaces exactly that span.

    Until migration 0099 the quote was resolved in a RENDERED projection with
    the markup stripped, so an agent quoting what it had actually read went
    stale the moment the span touched any markup. Quoting the source is now
    the contract, and the SPA captures in the same domain because its
    document IS the source."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)

        # Inside an inline mark: the delimiters are not part of the quote,
        # so they stay and only the word is replaced.
        nid, pid = await _note_with_part(c, h, "The **quick** brown fox.")
        acc = await _accept_suggestion(c, h, pid, "quick", "lazy")
        assert acc.status_code == 200, acc.text
        assert (await c.get(f"/notes/{nid}", headers=h)).json()["parts"][0][
            "body"
        ] == "The **lazy** brown fox."

        # A quote that INCLUDES the markup works too, which is the case the
        # rendered domain could not express at all: `**quick**` did not exist
        # in the projection an agent was made to search.
        nid, pid = await _note_with_part(c, h, "The **quick** brown fox.")
        acc = await _accept_suggestion(c, h, pid, "**quick**", "_slow_")
        assert acc.status_code == 200, acc.text
        assert (await c.get(f"/notes/{nid}", headers=h)).json()["parts"][0][
            "body"
        ] == "The _slow_ brown fox."

        # Link label: the destination is outside the quote and is preserved.
        nid, pid = await _note_with_part(c, h, "see [docs](http://x) now")
        acc = await _accept_suggestion(c, h, pid, "docs", "HERE")
        assert acc.status_code == 200, acc.text
        assert (await c.get(f"/notes/{nid}", headers=h)).json()["parts"][0][
            "body"
        ] == "see [HERE](http://x) now"

        # A deliberately MULTI-BLOCK quote still applies. The structural gate
        # exempts it: an anchor containing a blank line was written across a
        # boundary on purpose, and merging the paragraphs is the edit asked
        # for, not an inline edit restructuring the document by accident.
        nid, pid = await _note_with_part(c, h, "Para one here.\n\nPara two there.")
        acc = await _accept_suggestion(c, h, pid, "here.\n\nPara two", "X")
        assert acc.status_code == 200, acc.text
        assert (await c.get(f"/notes/{nid}", headers=h)).json()["parts"][0][
            "body"
        ] == "Para one X there."


async def test_accept_refuses_a_structural_change() -> None:
    """The gate that replaces the rendered path's re-render equality, asserted
    at the endpoint.

    A proposal can apply cleanly and still corrupt: `foo | bar` inside a
    two-column table row makes a three-cell row, and GFM renders only as many
    cells as the header has -- so the author's last cell silently disappears.
    markdown-it emits the same token stream either way, which is why the gate
    compares the rows' SOURCE cell counts too."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        table = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
        nid, pid = await _note_with_part(c, h, table)
        acc = await _accept_suggestion(c, h, pid, "1", "1 | X")
        assert acc.status_code == 400, acc.text
        assert "suggestion_stale" in acc.text
        # Refused means refused: the body is untouched.
        assert (await c.get(f"/notes/{nid}", headers=h)).json()["parts"][0]["body"] == table

        # A plain replacement in the same cell is fine.
        acc = await _accept_suggestion(c, h, pid, "1", "uno")
        assert acc.status_code == 200, acc.text
        assert (await c.get(f"/notes/{nid}", headers=h)).json()["parts"][0][
            "body"
        ] == "| a | b |\n| --- | --- |\n| uno | 2 |\n"


async def test_lifecycle_resolve_reopen_edit_delete() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        tid = (await c.post("/tasks", headers=h, json={"title": "T"})).json()["id"]
        a = (
            await c.post(
                "/annotations/comment",
                headers=h,
                json={"doc_kind": "task_description", "doc_id": tid, "body": "v1"},
            )
        ).json()
        aid = a["id"]

        # edit bumps version + stamps edited_at
        e = await c.patch(
            f"/annotations/{aid}", headers=h, json={"body": "v2", "expected_version": a["version"]}
        )
        assert e.status_code == 200, e.text
        edited = (await c.get(f"/annotations/{aid}", headers=h)).json()
        assert edited["body"] == "v2"
        assert edited["edited_at"] is not None

        # resolve then reopen
        r = await c.post(
            f"/annotations/{aid}/resolve", headers=h, json={"expected_version": edited["version"]}
        )
        assert r.status_code == 200, r.text
        resolved = (await c.get(f"/annotations/{aid}", headers=h)).json()
        assert resolved["status"] == "resolved"
        assert resolved["resolved_by_identity_id"] is not None
        ro = await c.post(
            f"/annotations/{aid}/reopen", headers=h, json={"expected_version": resolved["version"]}
        )
        assert ro.status_code == 200, ro.text
        assert (await c.get(f"/annotations/{aid}", headers=h)).json()["status"] == "open"

        # soft-delete hides it from the listing
        cur = (await c.get(f"/annotations/{aid}", headers=h)).json()
        d = await c.delete(
            f"/annotations/{aid}", headers=h, params={"expected_version": cur["version"]}
        )
        assert d.status_code == 200, d.text
        listed = (
            await c.get(
                "/annotations", headers=h, params={"doc_kind": "task_description", "doc_id": tid}
            )
        ).json()
        assert listed == []

        # ...and restore brings it back: the delete is soft on every
        # surface now, not just in the row.
        rs = await c.post(
            f"/annotations/{aid}/restore", headers=h, json={"expected_version": d.json()["version"]}
        )
        assert rs.status_code == 200, rs.text
        back = (
            await c.get(
                "/annotations", headers=h, params={"doc_kind": "task_description", "doc_id": tid}
            )
        ).json()
        assert [x["id"] for x in back] == [aid]


async def test_replace_in_annotation_body_over_rest() -> None:
    """Anchored find/replace on a comment body: the twin of the note-part
    replace, which the annotation family did not have at any scope. The
    no-op contract matches too (no version bump, stale cursor ignored)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        tid = (await c.post("/tasks", headers=h, json={"title": "T"})).json()["id"]
        a = (
            await c.post(
                "/annotations/comment",
                headers=h,
                json={
                    "doc_kind": "task_description",
                    "doc_id": tid,
                    "body": "ship friday, review friday",
                },
            )
        ).json()
        aid = a["id"]

        r = await c.post(
            f"/annotations/{aid}/body/replace",
            headers=h,
            json={"find": "friday", "replace": "monday", "expected_version": a["version"]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["replacements"] == 2
        assert r.json()["version"] == a["version"] + 1
        assert (await c.get(f"/annotations/{aid}", headers=h)).json()["body"] == (
            "ship monday, review monday"
        )

        noop = await c.post(
            f"/annotations/{aid}/body/replace",
            headers=h,
            json={"find": "absent", "replace": "x", "expected_version": 999},
        )
        assert noop.status_code == 200, noop.text
        assert noop.json()["replacements"] == 0
        assert noop.json()["version"] == a["version"] + 1  # unchanged


async def test_accept_rejects_a_plain_comment() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        tid = (await c.post("/tasks", headers=h, json={"title": "T"})).json()["id"]
        a = (
            await c.post(
                "/annotations/comment",
                headers=h,
                json={"doc_kind": "task_description", "doc_id": tid, "body": "note"},
            )
        ).json()
        r = await c.post(
            f"/annotations/{a['id']}/accept", headers=h, json={"expected_version": a["version"]}
        )
        assert r.status_code >= 400  # not a suggestion


async def test_cross_org_isolation() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h1 = await _signup(c)
        h2 = await _signup(c)
        tid = (await c.post("/tasks", headers=h1, json={"title": "secret"})).json()["id"]
        # org 2 cannot annotate org 1's task
        r = await c.post(
            "/annotations/comment",
            headers=h2,
            json={"doc_kind": "task_description", "doc_id": tid, "body": "peek"},
        )
        assert r.status_code == 404


async def test_ui_state_persists_collapse_per_card() -> None:
    """The SPA card collapse (migration 0084): no row = expanded; the
    per-card PUT persists the caller's state across fresh reads (list and
    single GET) and the upsert toggles back."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        tid = (await c.post("/tasks", headers=h, json={"title": "collapse me"})).json()["id"]
        a = (
            await c.post(
                "/annotations/comment",
                headers=h,
                json={"doc_kind": "task_description", "doc_id": tid, "body": "a long comment"},
            )
        ).json()
        assert a["ui_collapsed"] is False

        r = await c.put(f"/annotations/{a['id']}/ui-state", headers=h, json={"collapsed": True})
        assert r.status_code == 200, r.text
        assert r.json()["ui_collapsed"] is True

        rows = (
            await c.get(
                "/annotations", headers=h, params={"doc_kind": "task_description", "doc_id": tid}
            )
        ).json()
        assert [x["ui_collapsed"] for x in rows] == [True]
        assert (await c.get(f"/annotations/{a['id']}", headers=h)).json()["ui_collapsed"] is True

        r = await c.put(f"/annotations/{a['id']}/ui-state", headers=h, json={"collapsed": False})
        assert r.status_code == 200
        assert r.json()["ui_collapsed"] is False


async def test_ui_state_bulk_collapse_all_and_empty_doc() -> None:
    """``PUT /annotations/ui-state`` folds/unfolds every ROOT card on a
    document in one upsert (also pinning that the literal ``ui-state``
    segment is not captured as an annotation id). Replies keep their own
    state — folding a thread is the root's job — and an annotation-less
    document is a 200 [] no-op."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        _nid, pid = await _note_with_part(c, h, "Body under discussion.")
        roots = []
        for i in range(3):
            r = await c.post(
                "/annotations/comment",
                headers=h,
                json={"doc_kind": "note_part", "doc_id": pid, "body": f"c{i}"},
            )
            roots.append(r.json()["id"])
        reply_id = (
            await c.post(
                "/annotations/comment",
                headers=h,
                json={
                    "doc_kind": "note_part",
                    "doc_id": pid,
                    "body": "a reply",
                    "parent_id": roots[0],
                },
            )
        ).json()["id"]

        r = await c.put(
            "/annotations/ui-state",
            headers=h,
            json={"doc_kind": "note_part", "doc_id": pid, "collapsed": True},
        )
        assert r.status_code == 200, r.text
        state = {x["id"]: x["ui_collapsed"] for x in r.json()}
        assert [state[rid] for rid in roots] == [True, True, True]
        assert state[reply_id] is False  # replies untouched by collapse-all
        fresh = (
            await c.get("/annotations", headers=h, params={"doc_kind": "note_part", "doc_id": pid})
        ).json()
        assert {x["id"]: x["ui_collapsed"] for x in fresh} == state

        r = await c.put(
            "/annotations/ui-state",
            headers=h,
            json={"doc_kind": "note_part", "doc_id": pid, "collapsed": False},
        )
        assert all(x["ui_collapsed"] is False for x in r.json())

        _nid2, pid2 = await _note_with_part(c, h, "No comments here.")
        r = await c.put(
            "/annotations/ui-state",
            headers=h,
            json={"doc_kind": "note_part", "doc_id": pid2, "collapsed": True},
        )
        assert r.status_code == 200
        assert r.json() == []


async def test_ui_state_is_per_user_and_org_scoped() -> None:
    """Collapse state is the caller's own: a teammate in the same workspace
    still sees the card expanded; a stranger from another org gets a clean
    404 on the card id (RLS)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner_email = _email()
        owner = (
            await c.post("/auth/signup", json={"email": owner_email, "password": "pw-strong-123"})
        ).json()
        ws = owner["workspace_id"]
        oh = {
            "Authorization": f"Bearer {owner['token']}",
            "X-Workspace-Id": ws,
            "X-Workspace-Role": "owner",
        }
        guest_email = _email()
        guest = (
            await c.post("/auth/signup", json={"email": guest_email, "password": "pw-strong-123"})
        ).json()
        gh = {"Authorization": f"Bearer {guest['token']}", "X-Workspace-Id": ws}
        added = await c.post(
            "/workspaces/me/members", headers=oh, json={"email": guest_email, "role": "member"}
        )
        assert added.status_code == 200, added.text

        tid = (await c.post("/tasks", headers=oh, json={"title": "shared"})).json()["id"]
        a = (
            await c.post(
                "/annotations/comment",
                headers=oh,
                json={"doc_kind": "task_description", "doc_id": tid, "body": "hi"},
            )
        ).json()

        await c.put(f"/annotations/{a['id']}/ui-state", headers=oh, json={"collapsed": True})
        assert (await c.get(f"/annotations/{a['id']}", headers=oh)).json()["ui_collapsed"] is True
        assert (await c.get(f"/annotations/{a['id']}", headers=gh)).json()["ui_collapsed"] is False

        stranger = await _signup(c)
        r = await c.put(
            f"/annotations/{a['id']}/ui-state", headers=stranger, json={"collapsed": True}
        )
        assert r.status_code == 404


async def test_the_anchor_domain_is_declared_by_whoever_captured_it() -> None:
    """Two surfaces write the same three columns in two different languages,
    so the row says which one it is.

    An API, MCP or CLI caller reads the markdown SOURCE and quotes it, which
    is the default. The legacy WYSIWYG editor reads its anchor off a
    ProseMirror tree -- markup stripped, links reduced to their label -- and
    declares ``rendered``. Without the flag, one of the two would be silently
    read in the other's domain, which does not merely fail to locate: it can
    match the WRONG passage.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        body = "The **quick** brown fox."

        # The rendered quote has no asterisks and does not occur in the
        # source, so it only resolves when the row says which domain it is.
        nid, pid = await _note_with_part(c, h, "a **b** c")
        acc = await _accept_suggestion(c, h, pid, "b c", "X", anchor_domain="rendered")
        assert acc.status_code == 200, acc.text

        # The same quote WITHOUT the declaration is read as source, is not
        # there, and declines rather than matching something else.
        nid, pid = await _note_with_part(c, h, "a **b** c")
        acc = await _accept_suggestion(c, h, pid, "b c", "X")
        assert acc.status_code == 400, acc.text
        assert (await c.get(f"/notes/{nid}", headers=h)).json()["parts"][0]["body"] == "a **b** c"

        # And a source quote works with the default, which is the case the
        # rendered domain could not express at all.
        nid, pid = await _note_with_part(c, h, body)
        acc = await _accept_suggestion(c, h, pid, "**quick**", "_slow_")
        assert acc.status_code == 200, acc.text

        # The domain is readable back, so a client can tell what it is
        # looking at rather than guessing.
        rows = (await c.get(f"/annotations?doc_kind=note_part&doc_id={pid}", headers=h)).json()
        assert rows and all(r["anchor_domain"] in ("source", "rendered") for r in rows)
