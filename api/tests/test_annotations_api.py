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
    c: AsyncClient, h: dict[str, str], pid: str, original: str, proposed: str
):
    sug = (
        await c.post(
            "/annotations/suggestion",
            headers=h,
            json={
                "doc_kind": "note_part",
                "doc_id": pid,
                "original_text": original,
                "proposed_text": proposed,
            },
        )
    ).json()
    return await c.post(
        f"/annotations/{sug['id']}/accept", headers=h, json={"expected_version": sug["version"]}
    )


async def test_accept_is_markdown_aware() -> None:
    """End-to-end through the real stack: a suggestion whose rendered
    ``original_text`` (what the SPA captures) sits inside inline markup or
    spans blocks now splices faithfully into the markdown source — the old
    raw str.find would have gone stale."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)

        # inline mark: 'quick' is the rendered text inside **...**; accept
        # must keep the bold delimiters and replace only the word.
        nid, pid = await _note_with_part(c, h, "The **quick** brown fox.")
        acc = await _accept_suggestion(c, h, pid, "quick", "lazy")
        assert acc.status_code == 200, acc.text
        assert (await c.get(f"/notes/{nid}", headers=h)).json()["parts"][0][
            "body"
        ] == "The **lazy** brown fox."

        # link text: the URL is preserved, only the link label changes.
        nid, pid = await _note_with_part(c, h, "see [docs](http://x) now")
        acc = await _accept_suggestion(c, h, pid, "docs", "HERE")
        assert acc.status_code == 200, acc.text
        assert (await c.get(f"/notes/{nid}", headers=h)).json()["parts"][0][
            "body"
        ] == "see [HERE](http://x) now"

        # multi-block selection (rendered text joins blocks with a space).
        nid, pid = await _note_with_part(c, h, "Para one here.\n\nPara two there.")
        acc = await _accept_suggestion(c, h, pid, "here. Para two", "X")
        assert acc.status_code == 200, acc.text
        assert (await c.get(f"/notes/{nid}", headers=h)).json()["parts"][0][
            "body"
        ] == "Para one X there."


async def test_accept_straddle_swallows_run_formatting() -> None:
    """A selection that straddles an inline-mark edge (one delimiter inside the
    span, its partner outside) applies by swallowing the whole run and dropping
    the now-meaningless formatting -- render-faithful, never an orphaned
    delimiter. This is the deliberate contract of commit 8998deb; the splice
    layer is pinned by core/tests/test_md_anchor.py::test_splice_straddle_drops_formatting
    and the never-corrupts invariant by test_splice_never_corrupts_on_unmodellable_input.
    This case asserts it end-to-end through the accept endpoint."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        nid, pid = await _note_with_part(c, h, "a **b** c")
        acc = await _accept_suggestion(c, h, pid, "b c", "X Y")
        assert acc.status_code == 200, acc.text
        assert (await c.get(f"/notes/{nid}", headers=h)).json()["parts"][0]["body"] == "a X Y"


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
