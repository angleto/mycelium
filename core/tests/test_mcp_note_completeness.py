"""d3cefedf — note completeness: the projection exposes a note's lifecycle,
list_notes filters by maturity, and 'notes of a task' is one enriched call.

Audit #6b/#7/#9: an agent could not read a note's maturity/summary/dates
(projection omitted them, and the keep-filter made fields=['maturity']
return nothing), could not list 'the dormant notes' (maturity was
write-only), and 'notes of a task' was an N+1 (list links, then get_note
each).
"""

from __future__ import annotations

import uuid

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.note import Note
from mycelium_core.services.auth import signup
from mycelium_mcp.server import (
    create_note,
    create_task,
    create_task_note,
    get_note,
    list_notes,
    set_note_maturity,
)


async def _signup() -> tuple[str, str, uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="notes",
        )
    assert r.token is not None
    return r.token, str(r.org_id), r.org_id, r.user_id


async def test_note_projection_is_enriched_and_projectable() -> None:
    token, org, _oid, _uid = await _signup()
    n = await create_note(token=token, org_id=org, kind="text", text="hi", title="Enriched")

    full = await get_note(token=token, org_id=org, note_id=n["id"])
    assert full["maturity"] == "seed"  # always-set column, default
    assert full["is_archived"] is False
    assert "created_at" in full and "updated_at" in full
    # Unset nullable fields cost zero tokens (omitted, not null).
    assert "review_state" not in full and "summary" not in full

    listed = await list_notes(token=token, org_id=org)
    row = next(r for r in listed if r["id"] == n["id"])
    assert row["maturity"] == "seed"

    # The keep-filter now actually selects the new column.
    projected = await list_notes(token=token, org_id=org, fields=["maturity"])
    prow = next(r for r in projected if r["id"] == n["id"])
    assert prow["maturity"] == "seed"
    assert set(prow) <= {"id", "maturity"}


async def test_list_notes_filters_by_maturity() -> None:
    token, org, _oid, _uid = await _signup()
    n = await create_note(token=token, org_id=org, kind="text", text="x", title="Dormouse")
    await set_note_maturity(token=token, org_id=org, note_id=n["id"], maturity="dormant")

    dormant = await list_notes(token=token, org_id=org, maturity="dormant")
    assert any(r["id"] == n["id"] for r in dormant)
    assert all(r["maturity"] == "dormant" for r in dormant)
    # A different stage excludes it.
    seeds = await list_notes(token=token, org_id=org, maturity="seed")
    assert not any(r["id"] == n["id"] for r in seeds)


async def test_list_notes_by_task_returns_enriched_in_one_call() -> None:
    token, org, _oid, _uid = await _signup()
    task = await create_task(token=token, org_id=org, title="host task")
    a = await create_task_note(token=token, org_id=org, task_id=task["id"], title="note A")
    b = await create_task_note(token=token, org_id=org, task_id=task["id"], title="note B")

    rows = await list_notes(token=token, org_id=org, task_id=task["id"])
    ids = {r["id"] for r in rows}
    assert {a["id"], b["id"]} <= ids
    # Enriched, not just ids -> no per-note get_note needed.
    assert all("maturity" in r and "created_at" in r for r in rows)

    # Unknown task -> empty (never an error).
    assert await list_notes(token=token, org_id=org, task_id=str(uuid.uuid4())) == []


async def test_proposed_notes_excluded_even_via_task_path() -> None:
    token, org, _oid, user_id = await _signup()
    task = await create_task(token=token, org_id=org, title="host")
    n = await create_task_note(token=token, org_id=org, task_id=task["id"], title="pending review")
    # Drop it into the proposed-review state (the autonomous-generation state).
    async with tenant_session(org, str(user_id)) as s:
        note = await s.get(Note, uuid.UUID(n["id"]))
        assert note is not None
        note.review_state = "proposed"
        await s.flush()

    # Neither the general list nor the task-scoped path may surface it.
    assert not any(r["id"] == n["id"] for r in await list_notes(token=token, org_id=org))
    assert not any(
        r["id"] == n["id"] for r in await list_notes(token=token, org_id=org, task_id=task["id"])
    )
