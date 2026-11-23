"""Written backlinks over HTTP (ADR-0073): GET /garden/references/backlinks
and POST /garden/references/choose.

The projection is tested against the database in
``core/tests/test_body_references.py``; here the HTTP contract: a note
created through the API is a backlink of the note it cites with its
sentence, a missing target is a 404, an unknown kind is a 422, and choosing a
target for an occurrence that is not ambiguous is refused.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app
from mycelium_core.embedder import set_embedder_override


@pytest.fixture(autouse=True)
def _wire() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


async def _signup(c: AsyncClient) -> dict[str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={
                "email": f"{uuid.uuid4().hex[:10]}@example.test",
                "password": "pw-strong-123",
                "workspace_name": "REFS",
            },
        )
    ).json()
    return {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}


async def _note(c: AsyncClient, h: dict[str, str], text: str) -> str:
    r = await c.post("/notes", headers=h, json={"kind": "text", "text": text, "title": "N"})
    assert r.status_code in (200, 201), r.text
    return str(r.json()["id"])


async def test_backlinks_over_http() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _signup(c)
        target = await _note(c, h, "the cited note")
        citing = await _note(c, h, f"We rely on `{target[:8]}` here. Done.")
        r = await c.get(
            "/garden/references/backlinks",
            headers=h,
            params={"target_kind": "note", "target_id": target},
        )
        assert r.status_code == 200, r.text
        (row,) = r.json()["backlinks"]
        assert row["note_id"] == citing
        assert row["status"] == "resolved"
        assert row["syntax"] == "chip"
        assert (row["quote_prefix"], row["quote_exact"], row["quote_suffix"]) == (
            "We rely on ",
            target[:8],
            " here.",
        )
        assert row["also_typed"] is False

        missing = await c.get(
            "/garden/references/backlinks",
            headers=h,
            params={"target_kind": "note", "target_id": str(uuid.uuid4())},
        )
        assert missing.status_code == 404
        bad_kind = await c.get(
            "/garden/references/backlinks",
            headers=h,
            params={"target_kind": "user", "target_id": target},
        )
        assert bad_kind.status_code == 422

        refused = await c.post(
            "/garden/references/choose",
            headers=h,
            json={
                "reference_id": row["reference_id"],
                "target_kind": "note",
                "target_id": target,
            },
        )
        assert refused.status_code == 409, refused.text


async def test_link_proposal_round_trip_over_http() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _signup(c)
        a = await _note(c, h, "first idea")
        b = await _note(c, h, "second idea")
        sub = await c.post(
            "/garden/link-proposals",
            headers=h,
            json={
                "source_note_id": a,
                "target_note_id": b,
                "evidence": {"quote": "they share a premise"},
                "evidence_ref": "ext:42",
                "score": 0.9,
            },
        )
        assert sub.status_code == 200, sub.text
        pid = sub.json()["proposal_id"]
        q = await c.get("/garden/link-proposals", headers=h, params={"note_id": a})
        (item,) = q.json()["proposals"]
        assert item["proposal_id"] == pid
        assert "score" not in item
        shown = await c.post(
            "/garden/link-proposals/shown",
            headers=h,
            json={"proposal_ids": [pid], "surface": "note_panel"},
        )
        assert shown.json() == {"recorded": 1}
        acc = await c.post(
            "/garden/link-proposals/accept",
            headers=h,
            json={"proposal_id": pid, "kind": "related"},
        )
        assert acc.status_code == 200, acc.text
        links = (await c.get(f"/notes/{a}/links", headers=h)).json()
        (link,) = links["outgoing"] + links["incoming"]
        assert link["origin_proposer"].startswith("identity:")
        assert link["origin_evidence_ref"] == "ext:42"
        assert link["origin_proposal_id"] == pid
        assert link["created_by"] is not None
        again = await c.post("/garden/link-proposals/reject", headers=h, json={"proposal_id": pid})
        assert again.status_code == 409
        prec = (await c.get("/garden/link-proposals/precision", headers=h)).json()
        (row,) = prec["proposers"]
        assert (row["accepted"], row["shown"], row["precision"]) == (1, 1, 1.0)
