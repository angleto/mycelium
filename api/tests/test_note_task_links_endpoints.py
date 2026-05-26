"""Note↔task typed-link endpoints (task 892f40b1).

Covers the new note-side ``/notes/{id}/task-links`` POST/DELETE pair,
the task-side ``/tasks/{id}/note-links`` GET/POST/DELETE trio, and the
service-layer constraint that a ``promoted_from`` link cannot be
removed via DELETE (the promotion side-effect would be orphaned).
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
