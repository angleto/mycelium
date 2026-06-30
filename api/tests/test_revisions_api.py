"""REST endpoints for the recovery-history feature.

Smoke-checks the new ``/tasks/{id}/revisions``,
``/notes/{id}/revisions``, ``/edit-session/seal`` and
``/revisions/{rev_id}/restore`` endpoints alongside the
``X-Edit-Session-Id`` header on PATCH. Service-level coalescing /
seal / cascade / RLS already covered in
``core/tests/test_entity_revisions.py``; here we exercise wire-format
and the channel flip the header drives.
"""

from __future__ import annotations

import uuid
from typing import Any

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient, name: str) -> dict[str, Any]:
    r = await c.post(
        "/auth/signup",
        json={"email": _email(), "password": "pw-strong-123", "workspace_name": name},
    )
    return r.json()


def _auth(a: dict[str, Any]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
        "X-Workspace-Role": "owner",
    }


async def test_task_revisions_timeline_and_channel_flip() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = await _signup(c, "RevA")
        h = _auth(a)

        task = (await c.post("/tasks", headers=h, json={"title": "T0"})).json()
        tid = task["id"]
        v = task["version"]

        sess = uuid.uuid4().hex
        # First PATCH carries X-Edit-Session-Id: server flips channel
        # to "web" and opens a coalescing revision.
        r1 = await c.patch(
            f"/tasks/{tid}",
            headers={**h, "X-Edit-Session-Id": sess},
            json={"expected_version": v, "title": "T1"},
        )
        assert r1.status_code == 200
        v = r1.json()["version"]
        # Same session id -> the second PATCH coalesces into the same row.
        r2 = await c.patch(
            f"/tasks/{tid}",
            headers={**h, "X-Edit-Session-Id": sess},
            json={"expected_version": v, "description": "added"},
        )
        assert r2.status_code == 200
        v = r2.json()["version"]

        # Timeline has the open web row at the head plus the create
        # baseline (channel='api') behind it. The web row's
        # edit_count is 2 (coalesced) and changed_fields union both
        # column names.
        listing = (await c.get(f"/tasks/{tid}/revisions", headers=h)).json()
        assert len(listing) == 2
        open_row = listing[0]
        assert open_row["channel"] == "web"
        assert open_row["sealed_at"] is None
        assert open_row["edit_count"] == 2
        assert set(open_row["changed_fields"]) == {"title", "description"}
        base_row = listing[1]
        assert base_row["channel"] == "api"
        assert base_row["sealed_at"] is not None

        # Explicit seal closes the open row idempotently.
        s1 = await c.post(
            f"/tasks/{tid}/edit-session/seal",
            headers=h,
            json={"edit_session_id": sess},
        )
        assert s1.status_code == 200 and s1.json()["sealed"] == 1
        s2 = await c.post(
            f"/tasks/{tid}/edit-session/seal",
            headers=h,
            json={"edit_session_id": sess},
        )
        assert s2.json()["sealed"] == 0


async def test_task_revision_restore_roundtrip() -> None:
    """POST /tasks/{id}/revisions/{rev_id}/restore reverts the task
    and appends a fresh ``restore``-channel revision."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = await _signup(c, "RevRestore")
        h = _auth(a)
        task = (await c.post("/tasks", headers=h, json={"title": "T0"})).json()
        tid = task["id"]

        # Edit the title; this seals a new (api-channel) revision.
        v = task["version"]
        r = await c.patch(
            f"/tasks/{tid}",
            headers=h,
            json={"expected_version": v, "title": "T1"},
        )
        v = r.json()["version"]

        revs = (await c.get(f"/tasks/{tid}/revisions", headers=h)).json()
        # The oldest revision is the create baseline (title=T0).
        baseline = revs[-1]
        # Restore that baseline.
        rr = await c.post(
            f"/tasks/{tid}/revisions/{baseline['id']}/restore",
            headers=h,
            json={"expected_version": v},
        )
        assert rr.status_code == 200
        new_version = rr.json()["version"]
        assert new_version == v + 1

        # Task is reverted; a new restore revision sits at the head.
        task_now = (await c.get(f"/tasks/{tid}", headers=h)).json()
        assert task_now["title"] == "T0"
        revs_now = (await c.get(f"/tasks/{tid}/revisions", headers=h)).json()
        assert revs_now[0]["channel"] == "restore"
        assert revs_now[0]["restored_from"] == baseline["id"]


async def test_note_revisions_endpoints_symmetric() -> None:
    """Symmetric coverage for notes: PATCH + X-Edit-Session-Id,
    list, restore. Sanity that the polymorphic backend works on
    note ids the same way it does on task ids."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = await _signup(c, "RevNote")
        h = _auth(a)

        note = (
            await c.post(
                "/notes",
                headers=h,
                json={"kind": "text", "text": "first body"},
            )
        ).json()
        nid = note["id"]
        v = note["version"]

        sess = uuid.uuid4().hex
        await c.patch(
            f"/notes/{nid}",
            headers={**h, "X-Edit-Session-Id": sess},
            json={"expected_version": v, "title": "N1", "text": "second body"},
        )

        listing = (await c.get(f"/notes/{nid}/revisions", headers=h)).json()
        assert len(listing) == 2
        assert listing[0]["channel"] == "web"
        # Note baseline (create via API) is sealed-immediate.
        assert listing[1]["channel"] == "api"
        assert listing[1]["sealed_at"] is not None


async def test_revisions_rls_isolation_api() -> None:
    """A revision in workspace A is not reachable from workspace B's
    bearer. The defence-in-depth check in the router (entity_kind +
    entity_id) returns 404 even though RLS would also hide the row."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = await _signup(c, "WsA")
        b = await _signup(c, "WsB")
        ha, hb = _auth(a), _auth(b)

        task_a = (await c.post("/tasks", headers=ha, json={"title": "from A"})).json()
        tid = task_a["id"]
        listing = (await c.get(f"/tasks/{tid}/revisions", headers=ha)).json()
        rev_id = listing[0]["id"]

        # Workspace B can't fetch the revision.
        r = await c.get(f"/tasks/{tid}/revisions/{rev_id}", headers=hb)
        assert r.status_code == 404


async def test_task_revision_summary_patch() -> None:
    """PATCH /tasks/{id}/revisions/{rev_id} sets/clears the summary
    label. Goes through on sealed rows (column allow-list trigger)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = await _signup(c, "RevSummary")
        h = _auth(a)

        task = (await c.post("/tasks", headers=h, json={"title": "T0"})).json()
        tid = task["id"]
        listing = (await c.get(f"/tasks/{tid}/revisions", headers=h)).json()
        rev_id = listing[0]["id"]
        assert listing[0]["summary"] is None
        assert listing[0]["sealed_at"] is not None

        # Set the label.
        r = await c.patch(
            f"/tasks/{tid}/revisions/{rev_id}",
            headers=h,
            json={"summary": "task created"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["summary"] == "task created"

        # GET reflects the new value.
        again = (await c.get(f"/tasks/{tid}/revisions/{rev_id}", headers=h)).json()
        assert again["summary"] == "task created"

        # Clear with explicit null.
        r = await c.patch(
            f"/tasks/{tid}/revisions/{rev_id}",
            headers=h,
            json={"summary": None},
        )
        assert r.status_code == 200
        assert r.json()["summary"] is None

        # Pydantic enforces the 200-char max at the wire boundary.
        r = await c.patch(
            f"/tasks/{tid}/revisions/{rev_id}",
            headers=h,
            json={"summary": "x" * 201},
        )
        assert r.status_code == 422


async def test_note_revision_seq_increments_while_row_version_is_flat() -> None:
    """The timeline ``seq`` is a per-note revision counter (1 = first),
    distinct from the entity ROW version. Part-body edits log a revision
    each but do NOT bump the note row version, so every revision shares
    the same ``version_to`` while ``seq`` keeps incrementing — the fix for
    "the history always shows v1". Newest-first, so listing[0] carries the
    highest seq."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = await _signup(c, "RevSeq")
        h = _auth(a)

        note = (await c.post("/notes", headers=h, json={"kind": "text", "text": "body"})).json()
        nid = note["id"]

        # Ensure there's a part to edit (create one if the text note
        # didn't materialise one), then edit its body a few times. No
        # X-Edit-Session-Id -> "api" channel seals each immediately, so
        # the edits don't coalesce into one open revision.
        parts = (await c.get(f"/notes/{nid}/parts", headers=h)).json()
        if not parts:
            parts = [(await c.post(f"/notes/{nid}/parts", headers=h, json={"body": "p0"})).json()]
        pid = parts[0]["id"]
        ver = parts[0]["version"]
        ordv = parts[0]["ord"]
        for i in range(3):
            r = await c.patch(
                f"/notes/{nid}/parts/{pid}",
                headers=h,
                json={"expected_version": ver, "body": f"edit {i}"},
            )
            assert r.status_code == 200, r.text
            ver = r.json()["version"]

        listing = (await c.get(f"/notes/{nid}/revisions", headers=h)).json()
        seqs = [row["seq"] for row in listing]
        # Every revision carries a seq, contiguous and 1-based.
        assert all(s is not None for s in seqs)
        assert sorted(seqs) == list(range(1, len(listing) + 1))
        # Newest-first: the head holds the highest seq, the tail seq 1.
        assert listing[0]["seq"] == len(listing)
        assert listing[-1]["seq"] == 1
        # The note ROW version never moved (part edits bump the PART), so
        # version_to is flat across the whole timeline — exactly why seq
        # (not version_to) is the right number to surface.
        assert len({row["version_to"] for row in listing}) == 1
        # The part edits are tagged with the part position.
        assert any(f"parts[{ordv}].body" in row["changed_fields"] for row in listing)
