"""A write that answers with a task's tags answers with the tags it wrote.

Observed on 2026-11-27 (task 4012493f): ``create_task`` with a project tag
answered ``"tags": []``, and ``get_task`` right after showed the project and
the client derived from it. The rows were written; the answer was built by
the lean serializer, which took "not loaded" for "none" and printed an empty
list. An agent reading that answer concludes the project was not applied,
and every reaction it has is wrong: a second create (a duplicate), a
needless ``move_task_to_project``, or an error reported to a person that
did not happen.

So the rule these tests pin is the one that would have caught it: on every
MCP write that returns a task, a ``tags`` key, if there is one, says what a
read says right after. A tool that does not report the tags leaves the key
out rather than saying "none".
"""

from __future__ import annotations

import uuid
from typing import Any

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.task import Task
from mycelium_core.services.auth import signup
from mycelium_mcp.server import (
    _task,
    add_task_tag,
    create_client,
    create_project,
    create_tag,
    create_task,
    get_task,
    move_task_to_project,
    remove_task_tag,
    update_task,
)


async def _org() -> tuple[str, str]:
    token, org_id, _user = await _org_and_user()
    return token, org_id


async def _org_and_user() -> tuple[str, str, str]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="TAGS",
        )
    assert r.token is not None
    return r.token, str(r.org_id), str(r.user_id)


async def _project(token: str, org_id: str, name: str) -> tuple[str, str]:
    """A project under a client of its own: (project id, client id)."""
    client = await create_client(
        token=token, org_id=org_id, name=f"{name} client", legal_name=f"{name} Srl"
    )
    project = await create_project(
        token=token, org_id=org_id, name=name, client_tag_id=client["id"]
    )
    return project["id"], client["id"]


def _ids(tags: list[dict[str, Any]]) -> set[str]:
    return {t["id"] for t in tags}


async def _read_tags(token: str, org_id: str, task_id: str) -> set[str]:
    return _ids((await get_task(token=token, org_id=org_id, task_id=task_id))["tags"])


async def test_create_task_answers_the_project_and_the_client_it_derived() -> None:
    token, org_id = await _org()
    project, client = await _project(token, org_id, "Kiwi")

    created = await create_task(token=token, org_id=org_id, title="tagged", tag_ids=[project])

    read = await _read_tags(token, org_id, created["id"])
    assert {project, client} <= read
    assert _ids(created["tags"]) == read


async def test_create_task_with_no_tags_answers_the_defaults_it_landed_in() -> None:
    """No project passed: the task lands in General under Personal, and the
    answer says so instead of claiming the task has no tags."""
    token, org_id = await _org()

    created = await create_task(token=token, org_id=org_id, title="untagged")

    read = await _read_tags(token, org_id, created["id"])
    assert read, "a task always has a project and a client"
    assert _ids(created["tags"]) == read


async def test_no_task_write_answers_tags_that_a_read_contradicts() -> None:
    """The other writes on a task: each answer either leaves ``tags`` out or
    carries what ``get_task`` reads right after."""
    token, org_id = await _org()
    first, _ = await _project(token, org_id, "First")
    second, _ = await _project(token, org_id, "Second")
    facet = await create_tag(token=token, org_id=org_id, kind="generic", name="facet")
    created = await create_task(token=token, org_id=org_id, title="moved", tag_ids=[first])
    task_id = created["id"]

    answers = {
        "update_task": await update_task(
            token=token,
            org_id=org_id,
            task_id=task_id,
            expected_version=created["version"],
            title="moved and renamed",
        ),
        "add_task_tag": await add_task_tag(
            token=token, org_id=org_id, task_id=task_id, tag_id=facet["id"]
        ),
        "move_task_to_project": await move_task_to_project(
            token=token, org_id=org_id, task_id=task_id, project_tag_id=second
        ),
        "remove_task_tag": await remove_task_tag(
            token=token, org_id=org_id, task_id=task_id, tag_id=facet["id"]
        ),
    }

    read = await _read_tags(token, org_id, task_id)
    for tool, answer in answers.items():
        if "tags" in answer:
            assert _ids(answer["tags"]) == read, tool


async def test_the_lean_row_leaves_tags_out_when_it_was_not_given_them() -> None:
    """``whoami``'s open tasks are built without loading tags. The row must
    not say "no tags" about tags nobody read."""
    token, org_id, user_id = await _org_and_user()
    project, _ = await _project(token, org_id, "Lean")
    created = await create_task(token=token, org_id=org_id, title="lean", tag_ids=[project])

    async with tenant_session(org_id, user_id) as s:
        task = await s.get(Task, uuid.UUID(created["id"]))
        assert task is not None
        assert "tags" not in _task(task)
