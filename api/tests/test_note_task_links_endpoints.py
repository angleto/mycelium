"""Note↔task typed-link endpoints (task 892f40b1).

Covers the new note-side ``/notes/{id}/task-links`` POST/DELETE pair,
the task-side ``/tasks/{id}/note-links`` GET/POST/DELETE trio, and the
service-layer constraint that a ``promoted_from`` link cannot be
removed via DELETE (the promotion side-effect would be orphaned).
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


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
    r = await c.post(
        "/notes",
        headers=h,
        json={"kind": "text", "title": title, "text": f"body of {title}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _make_task(c: AsyncClient, h: dict[str, str], title: str) -> str:
    r = await c.post(
        "/tasks",
        headers=h,
        json={"title": title, "importance": 3, "urgency": 3},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_note_side_link_unlink_subject_artifact() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        note_id = await _make_note(c, h, "anchor")
        t1 = await _make_task(c, h, "work-it")
        t2 = await _make_task(c, h, "produced-it")

        r1 = await c.post(
            f"/notes/{note_id}/task-links",
            headers=h,
            json={"task_id": t1, "kind": "subject"},
        )
        assert r1.status_code == 200, r1.text
        r2 = await c.post(
            f"/notes/{note_id}/task-links",
            headers=h,
            json={"task_id": t2, "kind": "artifact"},
        )
        assert r2.status_code == 200, r2.text

        # Both visible via the existing /notes/{id}/links envelope.
        env = (await c.get(f"/notes/{note_id}/links", headers=h)).json()
        kinds = {(li["task_id"], li["kind"]) for li in env["task_links"]}
        assert (t1, "subject") in kinds
        assert (t2, "artifact") in kinds

        # Idempotent: a second POST returns the same link, not a 4xx.
        r3 = await c.post(
            f"/notes/{note_id}/task-links",
            headers=h,
            json={"task_id": t1, "kind": "subject"},
        )
        assert r3.status_code == 200

        # Delete subject; artifact stays.
        d = await c.delete(
            f"/notes/{note_id}/task-links",
            headers=h,
            params={"task_id": t1, "kind": "subject"},
        )
        assert d.status_code == 204
        env2 = (await c.get(f"/notes/{note_id}/links", headers=h)).json()
        remaining = {(li["task_id"], li["kind"]) for li in env2["task_links"]}
        assert (t1, "subject") not in remaining
        assert (t2, "artifact") in remaining

        # Deleting a missing row is a 404 (idempotency at the HTTP edge:
        # the service returns False, the router converts it).
        d2 = await c.delete(
            f"/notes/{note_id}/task-links",
            headers=h,
            params={"task_id": t1, "kind": "subject"},
        )
        assert d2.status_code == 404


async def test_task_side_link_unlink_mirrors_note_side() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        note_id = await _make_note(c, h, "n")
        task_id = await _make_task(c, h, "tk")

        r = await c.post(
            f"/tasks/{task_id}/note-links",
            headers=h,
            json={"note_id": note_id, "kind": "subject"},
        )
        assert r.status_code == 200, r.text

        # GET returns the wrapped {task_id, note_links: [...]} shape.
        g = await c.get(f"/tasks/{task_id}/note-links", headers=h)
        assert g.status_code == 200, g.text
        body = g.json()
        assert body["task_id"] == task_id
        assert len(body["note_links"]) == 1
        assert body["note_links"][0]["note_id"] == note_id
        assert body["note_links"][0]["kind"] == "subject"

        # Symmetric delete from the task side.
        d = await c.delete(
            f"/tasks/{task_id}/note-links",
            headers=h,
            params={"note_id": note_id, "kind": "subject"},
        )
        assert d.status_code == 204

        g2 = await c.get(f"/tasks/{task_id}/note-links", headers=h)
        assert g2.json()["note_links"] == []


async def test_promoted_from_unlink_is_refused() -> None:
    """Promotion sets ``note.promoted_at`` as a side-effect; removing
    the ``promoted_from`` link would orphan that timestamp. The service
    raises DomainError -> the router surfaces a 4xx, not a silent 204."""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        note_id = await _make_note(c, h, "to-promote")

        # Promote: a task is created with a promoted_from link.
        r = await c.post(
            f"/notes/{note_id}/promote",
            headers=h,
            json={"title": None},
        )
        assert r.status_code == 200, r.text
        task_id = r.json()["task_id"]

        # The link IS visible.
        env = (await c.get(f"/notes/{note_id}/links", headers=h)).json()
        kinds = {(li["task_id"], li["kind"]) for li in env["task_links"]}
        assert (task_id, "promoted_from") in kinds

        # But DELETE on it is refused (service-layer DomainError).
        d = await c.delete(
            f"/notes/{note_id}/task-links",
            headers=h,
            params={"task_id": task_id, "kind": "promoted_from"},
        )
        assert d.status_code >= 400 and d.status_code != 404, d.text


async def test_linked_task_count_includes_all_four_kinds() -> None:
    """Task 1e07437e: ``NoteOut.linked_task_count`` aggregates every
    kind of task link (subject, artifact, derived_from, promoted_from)
    so the SPA chip on NoteListItem matches the drawer panel.

    Earlier the chip showed only the count of ``derived_task_ids``,
    which omits subject/artifact links -- a note whose only link was
    ``subject`` showed no chip even though the drawer listed it.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)

        # Fresh note: no links yet -> chip-driver count is 0.
        note_id = await _make_note(c, h, "anchor")
        n0 = (await c.get(f"/notes/{note_id}", headers=h)).json()
        assert n0["linked_task_count"] == 0

        # Three different kinds of links, then re-read the note.
        # subject + artifact go through the typed endpoint;
        # derived_from is created as a side-effect of /derive-task.
        t_subj = await _make_task(c, h, "subject-task")
        t_art = await _make_task(c, h, "artifact-task")
        await c.post(
            f"/notes/{note_id}/task-links",
            headers=h,
            json={"task_id": t_subj, "kind": "subject"},
        )
        await c.post(
            f"/notes/{note_id}/task-links",
            headers=h,
            json={"task_id": t_art, "kind": "artifact"},
        )
        # /derive-task adds the derived_from edge (a new task is
        # created with the link in one shot).
        derived = await c.post(
            f"/notes/{note_id}/derive-task",
            headers=h,
            json={"title": "derived-task"},
        )
        assert derived.status_code == 200, derived.text

        # Single-note GET surfaces the count.
        n1 = (await c.get(f"/notes/{note_id}", headers=h)).json()
        assert n1["linked_task_count"] == 3, n1

        # The list endpoint surfaces the count too (the chip lives in
        # the list view, so the field must be populated there as well
        # -- and via a batched query, not N+1).
        listed = (await c.get("/notes", headers=h)).json()
        match = next(n for n in listed if n["id"] == note_id)
        assert match["linked_task_count"] == 3
        # And derived_task_ids stays exclusively the two "fruit" kinds
        # (here only the one ``derived_from`` task), so the SPA can
        # still show concrete task titles for that subset.
        assert len(match["derived_task_ids"]) == 1
