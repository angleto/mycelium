"""Proposal A: a note is the work log of exactly one task, with a
BIDIRECTIONAL note <-> task link. Billing still rolls up to the task
(time_entries.task_id stays NOT NULL; rate/billable live on the client
via task -> project -> client).

Covered here:
- start a timer with note_id -> entry.task_id == note.task_id AND
  entry.note_id == note.id;
- start with a note that has NO task -> 400 note.not_linked_to_task;
- PATCH entry preserves note_id when not passed; can set/clear it; the
  note<->task consistency rule is enforced on edit;
- PATCH note links then unlinks a task (optimistic concurrency);
- POST /tasks/{id}/notes creates a fresh note pre-linked to the task;
- cross-org isolation: a foreign note_id is rejected NOTE_NOT_FOUND on
  start; a foreign task_id is rejected TASK_NOT_FOUND on note link;
- the entries list / report carry note_id + note_title;
- deleting a note with billed time keeps the entry (ON DELETE SET
  NULL): note_id NULL, task_id intact (hard-delete to fire the FK).
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from flow_api.main import app
from flow_core.db import tenant_session


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> tuple[dict[str, str], dict[str, str]]:
    a = (await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})).json()
    h = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}
    return a, h


async def test_start_timer_with_note_derives_task_and_records_provenance() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        _, h = await _signup(c)

        task = (await c.post("/tasks", headers=h, json={"title": "Spec"})).json()
        tid = task["id"]
        # Work note pre-linked to the task (TASK side).
        note = (await c.post(f"/tasks/{tid}/notes", headers=h, json={})).json()
        assert note["task_id"] == tid
        nid = note["id"]

        # Start a timer by note only: the billing task is derived from
        # the note, and the note id is recorded as provenance.
        r = await c.post("/time/start", headers=h, json={"note_id": nid})
        assert r.status_code == 200, r.text
        e = r.json()
        assert e["task_id"] == tid
        assert e["note_id"] == nid
        assert e["note_title"] == note["title"]

        stopped = (await c.post("/time/stop", headers=h, json={})).json()
        assert stopped["note_id"] == nid
        assert stopped["task_id"] == tid

        # task_id + note_id together must agree (they do here).
        r2 = await c.post(
            "/time/entries",
            headers=h,
            json={
                "task_id": tid,
                "note_id": nid,
                "started_at": "2026-06-01T09:00:00+00:00",
                "ended_at": "2026-06-01T10:00:00+00:00",
            },
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["note_id"] == nid

        # Disagreeing task_id + note_id -> domain error.
        other = (await c.post("/tasks", headers=h, json={"title": "Other"})).json()
        rbad = await c.post(
            "/time/entries",
            headers=h,
            json={
                "task_id": other["id"],
                "note_id": nid,
                "started_at": "2026-06-02T09:00:00+00:00",
                "ended_at": "2026-06-02T10:00:00+00:00",
            },
        )
        assert rbad.status_code == 400, rbad.text
        assert rbad.json()["code"] == "domain.error"


async def test_start_with_unlinked_note_is_rejected() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        _, h = await _signup(c)
        # A plain note (no task link).
        note = (await c.post("/notes", headers=h, json={"kind": "text", "text": "x"})).json()
        assert note["task_id"] is None

        r = await c.post("/time/start", headers=h, json={"note_id": note["id"]})
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "note.not_linked_to_task"


async def test_patch_entry_note_id_preserve_set_clear_and_consistency() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        _, h = await _signup(c)

        task = (await c.post("/tasks", headers=h, json={"title": "T"})).json()
        tid = task["id"]
        note = (await c.post(f"/tasks/{tid}/notes", headers=h, json={})).json()
        nid = note["id"]

        # Plain entry on the task, no note yet.
        ent = (
            await c.post(
                "/time/entries",
                headers=h,
                json={
                    "task_id": tid,
                    "started_at": "2026-06-01T09:00:00+00:00",
                    "ended_at": "2026-06-01T10:00:00+00:00",
                },
            )
        ).json()
        eid = ent["id"]
        assert ent["note_id"] is None
        v = ent["version"]

        # A patch that does NOT mention note_id must preserve it (here:
        # still None) while changing billable.
        r = await c.patch(
            f"/time/entries/{eid}",
            headers=h,
            json={"expected_version": v, "billable": False},
        )
        assert r.status_code == 200, r.text
        v = r.json()["version"]
        assert (await c.get(f"/time/entries/{eid}", headers=h)).json()["note_id"] is None

        # Set note_id (note's task agrees with the entry's task).
        r = await c.patch(
            f"/time/entries/{eid}",
            headers=h,
            json={"expected_version": v, "note_id": nid},
        )
        assert r.status_code == 200, r.text
        v = r.json()["version"]
        got = (await c.get(f"/time/entries/{eid}", headers=h)).json()
        assert got["note_id"] == nid
        assert got["note_title"] == note["title"]

        # A subsequent unrelated patch preserves the now-set note_id.
        r = await c.patch(
            f"/time/entries/{eid}",
            headers=h,
            json={"expected_version": v, "billable": True},
        )
        assert r.status_code == 200, r.text
        v = r.json()["version"]
        assert (await c.get(f"/time/entries/{eid}", headers=h)).json()["note_id"] == nid

        # note<->task consistency on edit: reassign the entry to another
        # task while the linked note still belongs to the old one ->
        # domain error.
        other = (await c.post("/tasks", headers=h, json={"title": "Other"})).json()
        r = await c.patch(
            f"/time/entries/{eid}",
            headers=h,
            json={"expected_version": v, "task_id": other["id"]},
        )
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "domain.error"

        # Clear the link explicitly (null) -> note_id becomes None.
        r = await c.patch(
            f"/time/entries/{eid}",
            headers=h,
            json={"expected_version": v, "note_id": None},
        )
        assert r.status_code == 200, r.text
        got = (await c.get(f"/time/entries/{eid}", headers=h)).json()
        assert got["note_id"] is None
        assert got["note_title"] is None


async def test_patch_note_link_then_unlink_task_optimistic() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        _, h = await _signup(c)

        # Plain note BEFORE any task: the first note bootstraps the
        # workspace's default "Personal" client tag (a pre-existing
        # taxonomy ordering constraint also documented in
        # test_attachments.py; creating a task first would later make
        # the note's default-client insert collide).
        note = (await c.post("/notes", headers=h, json={"kind": "text", "text": "n"})).json()
        nid = note["id"]
        assert note["task_id"] is None
        v = note["version"]

        task = (await c.post("/tasks", headers=h, json={"title": "T"})).json()
        tid = task["id"]

        # Link the note to the task (NOTE side).
        r = await c.patch(
            f"/notes/{nid}",
            headers=h,
            json={"expected_version": v, "task_id": tid},
        )
        assert r.status_code == 200, r.text
        v = r.json()["version"]
        assert (await c.get(f"/notes/{nid}", headers=h)).json()["task_id"] == tid

        # Stale expected_version is rejected (optimistic concurrency).
        stale = await c.patch(
            f"/notes/{nid}",
            headers=h,
            json={"expected_version": v - 1, "task_id": None},
        )
        assert stale.status_code == 409, stale.text

        # A patch that omits task_id preserves the link (only title set).
        r = await c.patch(
            f"/notes/{nid}",
            headers=h,
            json={"expected_version": v, "title": "Renamed"},
        )
        assert r.status_code == 200, r.text
        v = r.json()["version"]
        assert (await c.get(f"/notes/{nid}", headers=h)).json()["task_id"] == tid

        # Unlink explicitly (null).
        r = await c.patch(
            f"/notes/{nid}",
            headers=h,
            json={"expected_version": v, "task_id": None},
        )
        assert r.status_code == 200, r.text
        assert (await c.get(f"/notes/{nid}", headers=h)).json()["task_id"] is None


async def test_post_task_notes_creates_linked_note() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        _, h = await _signup(c)
        task = (await c.post("/tasks", headers=h, json={"title": "Build it"})).json()
        tid = task["id"]

        # Default title = task title.
        n1 = (await c.post(f"/tasks/{tid}/notes", headers=h, json={})).json()
        assert n1["task_id"] == tid
        assert n1["title"] == "Build it"
        assert n1["kind"] == "text"
        # Not idempotent: a second call creates a *new* note.
        n2 = (
            await c.post(
                f"/tasks/{tid}/notes",
                headers=h,
                json={"title": "Second log", "text": "hello"},
            )
        ).json()
        assert n2["id"] != n1["id"]
        assert n2["task_id"] == tid
        assert n2["title"] == "Second log"
        assert n2["transcript"] == "hello"

        # Unknown task -> 404 TASK_NOT_FOUND.
        r = await c.post(f"/tasks/{uuid.uuid4()}/notes", headers=h, json={})
        assert r.status_code == 404, r.text
        assert r.json()["code"] == "task.not_found"


async def test_cross_org_isolation_note_and_task() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        # Org A: a task + a linked work note.
        _, ha = await _signup(c)
        task_a = (await c.post("/tasks", headers=ha, json={"title": "A task"})).json()
        note_a = (await c.post(f"/tasks/{task_a['id']}/notes", headers=ha, json={})).json()

        # Org B: separate workspace. Plain note first (default-client
        # bootstrap ordering, as above), then the task.
        _, hb = await _signup(c)
        note_b = (await c.post("/notes", headers=hb, json={"kind": "text", "text": "b"})).json()
        task_b = (await c.post("/tasks", headers=hb, json={"title": "B task"})).json()

        # B starting a timer with A's note id: RLS hides it ->
        # NOTE_NOT_FOUND (404).
        r = await c.post("/time/start", headers=hb, json={"note_id": note_a["id"]})
        assert r.status_code == 404, r.text
        assert r.json()["code"] == "note.not_found"

        # B linking its own note to A's task: RLS hides the task ->
        # TASK_NOT_FOUND (404).
        r = await c.patch(
            f"/notes/{note_b['id']}",
            headers=hb,
            json={"expected_version": note_b["version"], "task_id": task_a["id"]},
        )
        assert r.status_code == 404, r.text
        assert r.json()["code"] == "task.not_found"
        # B's own task link still works (sanity).
        r = await c.patch(
            f"/notes/{note_b['id']}",
            headers=hb,
            json={"expected_version": note_b["version"], "task_id": task_b["id"]},
        )
        assert r.status_code == 200, r.text


async def test_entries_list_and_report_carry_note_id_and_title() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        _, h = await _signup(c)
        task = (await c.post("/tasks", headers=h, json={"title": "Reported"})).json()
        tid = task["id"]
        note = (
            await c.post(
                f"/tasks/{tid}/notes",
                headers=h,
                json={"title": "Logged here"},
            )
        ).json()
        nid = note["id"]

        await c.post(
            "/time/entries",
            headers=h,
            json={
                "note_id": nid,
                "started_at": "2026-07-01T09:00:00+00:00",
                "ended_at": "2026-07-01T10:00:00+00:00",
            },
        )

        lst = (await c.get("/time/entries", headers=h, params={"task_id": tid})).json()
        assert len(lst) == 1
        assert lst[0]["note_id"] == nid
        assert lst[0]["note_title"] == "Logged here"
        # memo is the renamed free-text field (no note collision).
        assert "memo" in lst[0]
        assert "note" not in lst[0]


async def test_deleting_note_with_billed_time_keeps_entry_on_delete_set_null() -> None:
    """ON DELETE SET NULL: hard-deleting a note that has billed time
    must NOT delete the time entry. The entry survives with note_id
    NULL and task_id intact (the invoice still has its task). The API
    only soft-deletes notes; hard-delete the row directly (RLS-scoped
    session) to actually fire the FK action."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a, h = await _signup(c)
        ws, uid = a["workspace_id"], a["user_id"]

        task = (await c.post("/tasks", headers=h, json={"title": "Billed"})).json()
        tid = task["id"]
        note = (await c.post(f"/tasks/{tid}/notes", headers=h, json={})).json()
        nid = note["id"]

        ent = (
            await c.post(
                "/time/entries",
                headers=h,
                json={
                    "note_id": nid,
                    "started_at": "2026-08-01T09:00:00+00:00",
                    "ended_at": "2026-08-01T11:00:00+00:00",
                },
            )
        ).json()
        eid = ent["id"]
        assert ent["note_id"] == nid
        assert ent["task_id"] == tid

        async with tenant_session(ws, uid) as s:
            await s.execute(text("DELETE FROM notes WHERE id = :i"), {"i": nid})

        after = (await c.get(f"/time/entries/{eid}", headers=h)).json()
        # The entry is NOT deleted: only the note link is nulled.
        assert after["id"] == eid
        assert after["note_id"] is None
        assert after["note_title"] is None
        # Billing rollup is intact: the task is still there for invoicing.
        assert after["task_id"] == tid
        assert after["duration_seconds"] == 7200


async def test_note_serializer_carries_linked_task_title() -> None:
    """The work-note banner shows *which* task time is billed to:
    ``NoteOut.task_title`` is the linked task's title, resolved on
    create, GET, and list. Crucially it survives the task being archived
    -- a note linked to a closed task must not blank out, which a
    client-side ``/tasks`` lookup (filtered to live tasks) could not
    guarantee."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        _, h = await _signup(c)

        task = (await c.post("/tasks", headers=h, json={"title": "Onboard Acme"})).json()
        tid, tver = task["id"], task["version"]

        # Create path (POST /tasks/{id}/notes) carries the title.
        note = (await c.post(f"/tasks/{tid}/notes", headers=h, json={})).json()
        nid = note["id"]
        assert note["task_id"] == tid
        assert note["task_title"] == "Onboard Acme"

        # GET /notes/{id} (the deep link the banner opens) carries it.
        assert (await c.get(f"/notes/{nid}", headers=h)).json()["task_title"] == "Onboard Acme"

        # List endpoint carries it too (the list-click open path).
        lst = (await c.get("/notes", headers=h)).json()
        row = next(n for n in lst if n["id"] == nid)
        assert row["task_title"] == "Onboard Acme"

        # Archive the task: it drops out of the default task list, but
        # the note still reports the title (resolved server-side with no
        # lifecycle filter).
        r = await c.post(f"/tasks/{tid}/archive", headers=h, json={"expected_version": tver})
        assert r.status_code == 200, r.text
        assert (await c.get(f"/notes/{nid}", headers=h)).json()["task_title"] == "Onboard Acme"

        # A note with no task has task_title None.
        plain = (await c.post("/notes", headers=h, json={"kind": "text", "text": "x"})).json()
        assert plain["task_id"] is None
        assert plain["task_title"] is None
