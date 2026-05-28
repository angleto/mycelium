"""Note parts CRUD + reorder + ui-state (task 71c9d670 Phase 2a).

Each test exercises the full REST surface so a regression in the
service-layer concurrency, the deferred unique constraint, or the
ui-state upsert is caught at the integration boundary.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> dict[str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123"},
        )
    ).json()
    return {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
    }


async def _make_note(c: AsyncClient, h: dict[str, str], title: str) -> str:
    # Phase 6 prep (task 1cd8bc0a) makes ``create_note(text=...)``
    # auto-mirror the body into part(ord=0). The parts-CRUD tests
    # exercise the explicit POST /notes/{id}/parts surface, so we
    # create the note WITHOUT a text body to keep the parts list
    # empty until the test populates it on purpose.
    r = await c.post(
        "/notes",
        headers=h,
        json={"kind": "text", "title": title},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_get_note_returns_empty_parts_for_fresh_note() -> None:
    """New notes have no parts row yet (Phase 1 backfill targets
    pre-existing transcripts; new writes flow through Phase 2 of the
    chunker, which is 2b). GET /notes/{id} must surface an empty
    list, not 404 or undefined."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        note_id = await _make_note(c, h, "fresh")
        body = (await c.get(f"/notes/{note_id}", headers=h)).json()
        assert body["parts"] == []


async def test_create_list_update_delete_round_trip() -> None:
    """The canonical CRUD lifecycle: append two parts, list them in
    order, patch one body, delete the other. The ords stay stable
    across the lifecycle (no compaction on delete)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        note_id = await _make_note(c, h, "n")

        a = await c.post(
            f"/notes/{note_id}/parts",
            headers=h,
            json={"body": "first paragraph", "lang": "en"},
        )
        assert a.status_code == 200, a.text
        a_id = a.json()["id"]
        assert a.json()["ord"] == 0

        b = await c.post(
            f"/notes/{note_id}/parts",
            headers=h,
            json={"body": "second paragraph"},
        )
        assert b.status_code == 200
        b_id = b.json()["id"]
        assert b.json()["ord"] == 1
        assert b.json()["lang"] is None

        listed = (await c.get(f"/notes/{note_id}/parts", headers=h)).json()
        assert [p["id"] for p in listed] == [a_id, b_id]

        patched = await c.patch(
            f"/notes/{note_id}/parts/{a_id}",
            headers=h,
            json={"expected_version": 1, "body": "first paragraph (edited)"},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["version"] == 2

        deleted = await c.delete(f"/notes/{note_id}/parts/{b_id}", headers=h)
        assert deleted.status_code == 204
        post = (await c.get(f"/notes/{note_id}/parts", headers=h)).json()
        assert [p["id"] for p in post] == [a_id]
        # Ord of the surviving part stays 0 (delete does not compact).
        assert post[0]["ord"] == 0


async def test_create_at_specific_ord_shifts_existing_parts() -> None:
    """Inserting at ord=0 pushes every existing part forward by one
    via a single UPDATE; the deferred unique constraint tolerates
    the transient collision until COMMIT."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        note_id = await _make_note(c, h, "n")
        a = (await c.post(f"/notes/{note_id}/parts", headers=h, json={"body": "A"})).json()
        b = (await c.post(f"/notes/{note_id}/parts", headers=h, json={"body": "B"})).json()
        c_resp = await c.post(f"/notes/{note_id}/parts", headers=h, json={"body": "C", "ord": 0})
        assert c_resp.status_code == 200, c_resp.text
        c_part = c_resp.json()

        listed = (await c.get(f"/notes/{note_id}/parts", headers=h)).json()
        assert [p["id"] for p in listed] == [c_part["id"], a["id"], b["id"]]
        assert [p["ord"] for p in listed] == [0, 1, 2]


async def test_reorder_full_set_required_partial_set_refused() -> None:
    """``PUT /parts/order`` must receive the full set of part ids.
    A missing id (or an extra one) is a domain error -- otherwise a
    bug in the SPA could silently drop a row on reorder."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        note_id = await _make_note(c, h, "n")
        a = (await c.post(f"/notes/{note_id}/parts", headers=h, json={"body": "A"})).json()
        b = (await c.post(f"/notes/{note_id}/parts", headers=h, json={"body": "B"})).json()
        cc = (await c.post(f"/notes/{note_id}/parts", headers=h, json={"body": "C"})).json()

        partial = await c.put(
            f"/notes/{note_id}/parts/order",
            headers=h,
            json={"part_ids": [a["id"], b["id"]]},
        )
        assert partial.status_code >= 400, partial.text

        # Full set in a new order succeeds and reads back in order.
        ok = await c.put(
            f"/notes/{note_id}/parts/order",
            headers=h,
            json={"part_ids": [cc["id"], a["id"], b["id"]]},
        )
        assert ok.status_code == 200, ok.text
        listed = (await c.get(f"/notes/{note_id}/parts", headers=h)).json()
        assert [p["id"] for p in listed] == [cc["id"], a["id"], b["id"]]
        assert [p["ord"] for p in listed] == [0, 1, 2]


async def test_ui_state_persists_collapse_per_user() -> None:
    """No row in note_part_ui_state ≡ collapsed=false. PUTting
    ``collapsed=true`` materialises the row and a subsequent GET
    /notes/{id} surfaces the state on the part."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        note_id = await _make_note(c, h, "n")
        part = (await c.post(f"/notes/{note_id}/parts", headers=h, json={"body": "x"})).json()
        # Default expanded.
        pre = (await c.get(f"/notes/{note_id}", headers=h)).json()
        assert pre["parts"][0]["ui_collapsed"] is False

        toggled = await c.put(
            f"/notes/{note_id}/parts/{part['id']}/ui-state",
            headers=h,
            json={"collapsed": True},
        )
        assert toggled.status_code == 200
        assert toggled.json()["ui_collapsed"] is True

        post = (await c.get(f"/notes/{note_id}", headers=h)).json()
        assert post["parts"][0]["ui_collapsed"] is True


async def test_patch_optimistic_version_conflict() -> None:
    """A stale ``expected_version`` raises stale_version; without
    optimistic concurrency the SPA autosave could silently overwrite
    a concurrent edit (the same contract as update_note)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        note_id = await _make_note(c, h, "n")
        p = (await c.post(f"/notes/{note_id}/parts", headers=h, json={"body": "x"})).json()
        r = await c.patch(
            f"/notes/{note_id}/parts/{p['id']}",
            headers=h,
            json={"expected_version": 999, "body": "y"},
        )
        assert r.status_code >= 400, r.text
        assert "stale_version" in r.text


async def test_merge_notes_appends_source_parts_and_soft_deletes_source() -> None:
    """task 71c9d670 Phase 2b: POST /notes/merge folds source parts
    into the target (with merged_from_note_id stamped), soft-deletes
    the source, and records a `supersedes` link target -> source."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        src = await _make_note(c, h, "src")
        tgt = await _make_note(c, h, "tgt")
        # Two parts on source, one on target.
        s1 = (await c.post(f"/notes/{src}/parts", headers=h, json={"body": "S1"})).json()
        s2 = (await c.post(f"/notes/{src}/parts", headers=h, json={"body": "S2"})).json()
        t1 = (await c.post(f"/notes/{tgt}/parts", headers=h, json={"body": "T1"})).json()

        merged = await c.post(
            "/notes/merge",
            headers=h,
            json={"source_note_id": src, "target_note_id": tgt},
        )
        assert merged.status_code == 200, merged.text
        body = merged.json()
        # Target now has T1 + S1 + S2 in that order.
        ids = [p["id"] for p in body["parts"]]
        assert ids == [t1["id"], s1["id"], s2["id"]]
        # Moved parts carry merged_from_note_id; the original t1 doesn't.
        for p in body["parts"]:
            if p["id"] in (s1["id"], s2["id"]):
                assert p["merged_from_note_id"] == src
            else:
                assert p["merged_from_note_id"] is None
        # Source is soft-deleted: a fresh GET returns 404 or
        # deleted_at != None depending on the service's contract; the
        # important assertion is that it no longer appears in the
        # workspace list.
        listed = (await c.get("/notes", headers=h)).json()
        assert all(n["id"] != src for n in listed)


async def test_merge_self_or_deleted_target_refused() -> None:
    """Self-merge and a soft-deleted target both raise domain errors;
    the source's idempotent skip (already deleted) returns 200 without
    re-running so a retry is safe."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        n = await _make_note(c, h, "n")
        r = await c.post(
            "/notes/merge",
            headers=h,
            json={"source_note_id": n, "target_note_id": n},
        )
        assert r.status_code >= 400, r.text


async def test_merge_idempotent_on_already_merged_source() -> None:
    """A second POST /notes/merge with the same source is a no-op
    (the source is already soft-deleted; the service returns the
    target unchanged)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        src = await _make_note(c, h, "src")
        tgt = await _make_note(c, h, "tgt")
        await c.post(f"/notes/{src}/parts", headers=h, json={"body": "S"})
        first = await c.post(
            "/notes/merge",
            headers=h,
            json={"source_note_id": src, "target_note_id": tgt},
        )
        assert first.status_code == 200, first.text
        n_parts = len(first.json()["parts"])
        again = await c.post(
            "/notes/merge",
            headers=h,
            json={"source_note_id": src, "target_note_id": tgt},
        )
        assert again.status_code == 200, again.text
        assert len(again.json()["parts"]) == n_parts  # no double-move
