"""F1 MCP co-equality (DB-backed): MCP tools reuse the same service
layer as REST (docs/adr/0001)."""

from __future__ import annotations

import uuid

from mycelium_core.db import admin_session
from mycelium_core.services.auth import signup
from mycelium_mcp.server import (
    add_checklist_item,
    create_tag,
    create_task,
    get_task,
    list_tasks,
)


async def test_mcp_tools_reuse_service_layer() -> None:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP",
        )
    token, org = r.token, str(r.org_id)
    tag = await create_tag(token=token, org_id=org, kind="generic", name="mcp-tag")
    await create_task(token=token, org_id=org, title="via-mcp", tag_ids=[tag["id"]])
    titles = [t["title"] for t in await list_tasks(token=token, org_id=org)]
    assert "via-mcp" in titles


async def test_get_task_and_list_carry_tags() -> None:
    """Regression: get_task / list_tasks must surface a task's tags so
    an MCP caller can see them without a separate list_tags round-trip."""
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP-tags",
        )
    token, org = r.token, str(r.org_id)
    tag = await create_tag(token=token, org_id=org, kind="generic", name="surf-tag")
    created = await create_task(token=token, org_id=org, title="tagged", tag_ids=[tag["id"]])

    full = await get_task(token=token, org_id=org, task_id=created["id"])
    names = {g["name"] for g in full["tags"]}
    assert "surf-tag" in names
    surf = next(g for g in full["tags"] if g["name"] == "surf-tag")
    assert {"id", "kind", "name", "color"} <= surf.keys()

    listed = next(t for t in await list_tasks(token=token, org_id=org) if t["id"] == created["id"])
    assert "surf-tag" in {g["name"] for g in listed["tags"]}


async def test_list_tasks_free_text_q() -> None:
    """``q`` filters list_tasks server-side over title, description,
    checklist text and tag name; whitespace terms are ANDed (eb874772)."""
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP-q",
        )
    token, org = r.token, str(r.org_id)
    tag = await create_tag(token=token, org_id=org, kind="generic", name="invoice-tag")
    by_title = await create_task(token=token, org_id=org, title="Send the invoice")
    by_desc = await create_task(
        token=token, org_id=org, title="Quarterly review", description="chase the invoice"
    )
    by_check = await create_task(token=token, org_id=org, title="Sprint planning")
    await add_checklist_item(
        token=token, org_id=org, task_id=by_check["id"], text="draft invoice email"
    )
    by_tag = await create_task(token=token, org_id=org, title="Tagged item", tag_ids=[tag["id"]])
    noise = await create_task(token=token, org_id=org, title="Unrelated chore")

    async def ids_for(q: str) -> set[str]:
        return {t["id"] for t in await list_tasks(token=token, org_id=org, q=q)}

    # A single term matches across all four fields, never the noise row.
    hit = await ids_for("invoice")
    assert {by_title["id"], by_desc["id"], by_check["id"], by_tag["id"]} <= hit
    assert noise["id"] not in hit
    # Case-insensitive.
    assert by_title["id"] in await ids_for("INVOICE")
    # Terms are ANDed: only the checklist row carries BOTH words.
    assert await ids_for("invoice email") == {by_check["id"]}
    # A term present nowhere returns nothing.
    assert await ids_for("zzqqxx") == set()
