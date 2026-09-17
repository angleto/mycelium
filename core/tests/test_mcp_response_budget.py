"""A ceiling on what a result costs, so the bytes cannot come back quietly.

``test_mcp_payload_economy.py`` pins the individual decisions: no ranking
floats, no repeated workflow id, a capped description, short entity ids. Each
of those is one assertion about one key, and a shape assertion cannot see the
thing that actually went wrong historically -- a field added here, a nested
object widened there, none of them individually worth arguing about, and the
tool is twice the size a year later.

So this file measures instead. It serializes a fixed input through the real
tools and asserts the result fits a budget. The budgets are recorded
measurements plus headroom, and they move DOWN: a change that needs one raised
is a change that decided to spend more, which is a decision worth writing in a
diff rather than discovering in a usage report.

The unit is bytes of compact JSON, which is what the wire carries. Tokens are
the thing anyone cares about, but the byte-to-token ratio is corpus-dependent
(``billing.BYTES_PER_TOKEN``, measured at 2.87 on this surface) and pinning a
tokenizer here would make the suite depend on one.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mycelium_core.db import admin_session
from mycelium_core.services.auth import signup
from mycelium_mcp.gateway import execute_tool
from mycelium_mcp.server import _PRINCIPAL

#: One lean task row on the wire, for a task with a 60-character title and the
#: two tags a fresh org gives it. 603 bytes before this work; **557 now**, and
#: the budget is that plus modest headroom.
#:
#: This number went UP once during the change and the raise is the point of
#: writing it down. The row first went to 472 by dropping ``workflow_id`` and
#: the ``assignee_id`` / ``owner_id`` uuids outright, and dropping those two
#: turned out to remove a capability two other tests pinned: a caller could no
#: longer see whose a task was without a per-row ``get_task``. The row now
#: carries the resolved HANDLE instead, which answers the same question, is
#: what ``set_task_assignee`` takes back, and a person can read. So 557 buys
#: something 472 did not, and it is still below where this started.
#:
#: The fixture's handle is an email-derived 23-character one; a real handle
#: (``angelo``, ``claude``) is a third of that, so a production row is smaller
#: than this ceiling suggests.
#:
#: Most of what is left is the two tag objects, ~250 bytes, identical on every
#: row of a page. Hoisting a per-response constant out of the rows needs an
#: envelope key that ``test_mcp_pagination_general.py`` pins shut, so it is a
#: separate decision and not a drive-by; recorded here because this number is
#: where somebody will next come looking.
_LEAN_ROW_BYTES = 600

#: ``get_task`` on a task whose description is 30 KB. The cap is what makes
#: this a constant instead of a function of the description: measured at 1941
#: bytes on 2026-09-17, against ~31,000 before the cap.
_FULL_TASK_BYTES = 2100


def _size(payload: Any) -> int:
    return len(json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"))


@asynccontextmanager
async def _principal() -> AsyncIterator[None]:
    """A fresh org, entered as the gateway's authenticated principal.

    Through the gateway on purpose: the budget is about what the client
    receives, and the short-id convention that shrinks it is a property of
    that surface rather than of the serializers."""
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="BUDGET",
        )
    reset = _PRINCIPAL.set((r.user_id, r.org_id, None))
    try:
        yield
    finally:
        _PRINCIPAL.reset(reset)


async def _task_with_long_brief() -> dict[str, Any]:
    created = await execute_tool(name="create_task", arguments={"title": "a task with a brief"})
    await execute_tool(
        name="update_task",
        arguments={
            "task_id": created["id"],
            "expected_version": created["version"],
            "description": "d" * 30_000,
        },
    )
    return created


async def test_a_lean_task_row_fits_its_budget() -> None:
    """The row is the unit that multiplies: a page is 50 of these by default
    and was seen at 200. Anything added here is added 200 times."""
    async with _principal():
        await execute_tool(name="create_task", arguments={"title": "t" * 60})
        page = await execute_tool(name="list_tasks", arguments={})
    row = page["items"][0]
    assert _size(row) <= _LEAN_ROW_BYTES, (
        f"a lean task row is now {_size(row)} bytes, over the {_LEAN_ROW_BYTES} budget: "
        f"{sorted(row)}"
    )


async def test_reading_one_task_does_not_scale_with_its_description() -> None:
    """The property the cap exists for, stated as the thing a caller feels:
    the cost of reading a task is bounded by the task's SHAPE, not by how much
    someone wrote in it. A 30 KB design note used to land whole in the
    conversation and stay there for the rest of the session."""
    async with _principal():
        created = await _task_with_long_brief()
        got = await execute_tool(name="get_task", arguments={"task_id": created["id"]})
    assert _size(got) <= _FULL_TASK_BYTES, f"get_task is now {_size(got)} bytes"
    # And the caller can still tell that it was cut, and by how much.
    assert got["description_truncated"] is True
    assert got["description_chars"] == 30_000


async def test_the_escape_hatch_is_not_budgeted() -> None:
    """The bound above must not be enforceable against a caller that asked for
    the whole text: a cap that cannot be opted out of is data loss wearing an
    optimization's clothes."""
    async with _principal():
        created = await _task_with_long_brief()
        got = await execute_tool(
            name="get_task",
            arguments={"task_id": created["id"], "full_description": True},
        )
    assert _size(got) > _FULL_TASK_BYTES
    assert len(got["description"]) == 30_000
