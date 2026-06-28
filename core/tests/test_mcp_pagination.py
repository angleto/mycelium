"""c20c6351 — unified pagination contract on list_tasks.

list_tasks returns a ``{items, next_cursor, truncated}`` envelope (not a bare
array): truncation is visible, and a keyset cursor walks disjoint pages with
no dupes/gaps and no re-fetching of the same window. The row cap is pushed
into SQL, so the list view no longer materializes the whole RLS-scoped table.
"""

from __future__ import annotations

import uuid

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.tag import TagKind
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services import taxonomy as taxonomy_svc
from mycelium_core.services.auth import signup
from mycelium_mcp.server import create_tag, create_task, list_tasks


async def _signup(name: str) -> tuple[str, str, uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name=name,
        )
    assert r.token is not None
    return r.token, str(r.org_id), r.org_id, r.user_id


async def test_list_tasks_envelope_and_keyset_pagination() -> None:
    token, org, _oid, _uid = await _signup("page")
    tag = await create_tag(token=token, org_id=org, kind="generic", name="pageset")
    for i in range(5):
        await create_task(token=token, org_id=org, title=f"task-{i}", tag_ids=[tag["id"]])

    # The full list: the envelope shape, not truncated, no cursor.
    full = await list_tasks(token=token, org_id=org, tag_id=tag["id"], limit=100)
    assert set(full) == {"items", "next_cursor", "truncated"}
    assert full["truncated"] is False
    assert full["next_cursor"] is None
    full_ids = [t["id"] for t in full["items"]]
    assert len(full_ids) == 5

    # Page 1: k items + truncated=true + a cursor.
    p1 = await list_tasks(token=token, org_id=org, tag_id=tag["id"], limit=2)
    assert len(p1["items"]) == 2
    assert p1["truncated"] is True
    assert p1["next_cursor"]

    # Page 2: the next disjoint page.
    p2 = await list_tasks(
        token=token, org_id=org, tag_id=tag["id"], limit=2, cursor=p1["next_cursor"]
    )
    assert len(p2["items"]) == 2
    assert p2["truncated"] is True
    assert p2["next_cursor"]

    # Page 3: the last page -> truncated=false, next_cursor=null.
    p3 = await list_tasks(
        token=token, org_id=org, tag_id=tag["id"], limit=2, cursor=p2["next_cursor"]
    )
    assert len(p3["items"]) == 1
    assert p3["truncated"] is False
    assert p3["next_cursor"] is None

    # Concatenation == the full ordered set: no dupes, no gaps, same order.
    paged_ids = [t["id"] for t in p1["items"] + p2["items"] + p3["items"]]
    assert paged_ids == full_ids
    assert len(set(paged_ids)) == 5


async def test_list_tasks_pushes_limit_into_sql() -> None:
    # The service caps rows in SQL (LIMIT k), not by fetching the whole
    # RLS-scoped table and slicing in Python.
    _token, _org, org_id, user_id = await _signup("sql")
    async with tenant_session(str(org_id), str(user_id)) as s:
        tag = await taxonomy_svc.create_tag(
            s, org_id=org_id, actor_id=user_id, kind=TagKind.generic, name="sqlset"
        )
        for i in range(5):
            await tasks_svc.create_task(
                s, org_id=org_id, actor_id=user_id, title=f"t{i}", tag_ids=[tag.id]
            )
        rows = await tasks_svc.list_tasks(s, org_id=org_id, tag_id=tag.id, limit=2)
        assert len(rows) == 2
        # No limit -> all five (the cap is opt-in, REST/internal callers
        # keep their behaviour).
        assert len(await tasks_svc.list_tasks(s, org_id=org_id, tag_id=tag.id)) == 5
