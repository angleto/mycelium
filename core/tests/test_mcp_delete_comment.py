"""delete_comment — the inverse of add_comment in the tasks domain.

Task work-diary comments are Annotation rows, so a soft-delete already
existed as ``delete_annotation`` (domain 'misc'). ``delete_comment`` is the
task-vocabulary alias over the same ``annotations.soft_delete`` service, so a
caller who thinks in add_comment/list_comments terms can find and use it. It
carries the same optimistic-version guard and author-or-admin authorization.
"""

from __future__ import annotations

import uuid

import pytest

from mycelium_core.db import admin_session
from mycelium_core.errors import ConflictError
from mycelium_core.services.auth import signup
from mycelium_mcp.server import (
    add_comment,
    create_task,
    delete_comment,
    list_comments,
)


async def _signup(name: str) -> tuple[str, str]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name=name,
        )
    assert r.token is not None
    return r.token, str(r.org_id)


async def test_add_comment_returns_version() -> None:
    # The version is what delete_comment needs; add_comment must surface it so
    # the add -> delete loop is self-contained (no list_comments round-trip).
    token, org = await _signup("dc-ver")
    task = await create_task(token=token, org_id=org, title="t")
    c = await add_comment(token=token, org_id=org, task_id=task["id"], body="hi")
    assert c["task_id"] == task["id"]
    assert isinstance(c["version"], int)


async def test_delete_comment_soft_deletes_from_list() -> None:
    token, org = await _signup("dc-del")
    task = await create_task(token=token, org_id=org, title="t")
    c = await add_comment(token=token, org_id=org, task_id=task["id"], body="to-delete")

    before = await list_comments(token=token, org_id=org, task_id=task["id"])
    assert [row["id"] for row in before] == [c["id"]]

    out = await delete_comment(
        token=token, org_id=org, comment_id=c["id"], expected_version=c["version"]
    )
    assert out == {"id": c["id"], "version": c["version"] + 1, "deleted": True}

    after = await list_comments(token=token, org_id=org, task_id=task["id"])
    assert c["id"] not in [row["id"] for row in after]


async def test_delete_comment_stale_version_conflicts() -> None:
    # The optimistic guard: a wrong expected_version must refuse, not delete.
    token, org = await _signup("dc-stale")
    task = await create_task(token=token, org_id=org, title="t")
    c = await add_comment(token=token, org_id=org, task_id=task["id"], body="guarded")

    with pytest.raises(ConflictError):
        await delete_comment(
            token=token, org_id=org, comment_id=c["id"], expected_version=c["version"] + 99
        )

    still = await list_comments(token=token, org_id=org, task_id=task["id"])
    assert c["id"] in [row["id"] for row in still]
