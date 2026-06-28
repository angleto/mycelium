"""080a9c13 — shared filter vocabulary + count/exists + bounded org-wide
lists + self-reference.

The thread: an agent should answer "how many open tasks", "do any tasks
tagged X exist", "open comments on this doc", "what am I tracking now"
WITHOUT first resolving a non-terminal state uuid and without fetching a
list only to ``len()`` it. These guard exactly those routes.
"""

from __future__ import annotations

import uuid

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.tag import TagKind
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services import taxonomy as taxonomy_svc
from mycelium_core.services import workflow as workflow_svc
from mycelium_core.services.auth import signup
from mycelium_mcp.server import (
    add_comment,
    add_dependency,
    add_task_relation,
    count_annotations,
    count_tasks,
    create_tag,
    create_task,
    list_annotations,
    list_dependencies,
    list_running_timers,
    list_task_relations,
    list_tasks,
    start_timer,
)


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


async def test_open_only_filter_and_count_tasks() -> None:
    """``open_only`` keeps only non-terminal-state tasks (resolved via
    WorkflowState.is_terminal, no prior state-uuid lookup), and
    ``count_tasks`` equals ``len(list_tasks)`` for the same filters."""
    _tok, _org, org_id, user_id = await _signup("disc-open")
    async with tenant_session(str(org_id), str(user_id)) as s:
        tag = await taxonomy_svc.create_tag(
            s, org_id=org_id, actor_id=user_id, kind=TagKind.generic, name="open-set"
        )
        ids = []
        for title in ("a", "b", "c"):
            t = await tasks_svc.create_task(
                s, org_id=org_id, actor_id=user_id, title=title, tag_ids=[tag.id]
            )
            ids.append(t.id)
        # Drop one task into a terminal state directly (the filter is under
        # test, not the transition machine), via WorkflowState.is_terminal.
        wf = await workflow_svc.get_default_workflow(s, org_id)
        states = await workflow_svc.get_states(s, wf.id)
        terminal = next(st for st in states if st.is_terminal)
        closed = await tasks_svc.get_task(s, org_id=org_id, task_id=ids[0])
        closed.state_id = terminal.id
        await s.flush()

        listed = await tasks_svc.list_tasks(s, org_id=org_id, tag_id=tag.id)
        open_listed = await tasks_svc.list_tasks(s, org_id=org_id, tag_id=tag.id, open_only=True)
        assert len(listed) == 3
        assert {t.id for t in open_listed} == {ids[1], ids[2]}

        # count mirrors list for the same filters, but is a COUNT query.
        assert await tasks_svc.count_tasks(s, org_id=org_id, tag_id=tag.id) == 3
        assert await tasks_svc.count_tasks(s, org_id=org_id, tag_id=tag.id, open_only=True) == 2


async def test_count_tasks_tool_matches_list_tool() -> None:
    token, org, _oid, _uid = await _signup("disc-count")
    tag = await create_tag(token=token, org_id=org, kind="generic", name="count-set")
    for title in ("x", "y", "z"):
        await create_task(token=token, org_id=org, title=title, tag_ids=[tag["id"]])
    listed = (await list_tasks(token=token, org_id=org, tag_id=tag["id"]))["items"]
    counted = await count_tasks(token=token, org_id=org, tag_id=tag["id"])
    assert counted == {"total": 3}
    assert counted["total"] == len(listed)


async def test_count_annotations_and_kind_filter() -> None:
    token, org, _oid, _uid = await _signup("disc-annot")
    task = await create_task(token=token, org_id=org, title="commented")
    await add_comment(token=token, org_id=org, task_id=task["id"], body="first")
    await add_comment(token=token, org_id=org, task_id=task["id"], body="second")

    counts = await count_annotations(
        token=token, org_id=org, doc_kind="task_description", doc_id=task["id"]
    )
    assert counts == {"total": 2, "open": 2}

    comments = await list_annotations(
        token=token, org_id=org, doc_kind="task_description", doc_id=task["id"], kind="comment"
    )
    assert len(comments) == 2
    # No suggestions on a task description: the kind filter must isolate.
    sug_count = await count_annotations(
        token=token, org_id=org, doc_kind="task_description", doc_id=task["id"], kind="suggestion"
    )
    assert sug_count == {"total": 0, "open": 0}
    sug_list = await list_annotations(
        token=token, org_id=org, doc_kind="task_description", doc_id=task["id"], kind="suggestion"
    )
    assert sug_list == []


async def test_org_wide_edge_lists_are_bounded() -> None:
    token, org, _oid, _uid = await _signup("disc-edges")
    a = await create_task(token=token, org_id=org, title="ea")
    b = await create_task(token=token, org_id=org, title="eb")
    c = await create_task(token=token, org_id=org, title="ec")
    await add_dependency(
        token=token, org_id=org, predecessor_id=a["id"], successor_id=b["id"], type="FS"
    )
    await add_dependency(
        token=token, org_id=org, predecessor_id=b["id"], successor_id=c["id"], type="FS"
    )
    await add_task_relation(token=token, org_id=org, task_id=a["id"], other_id=b["id"])
    await add_task_relation(token=token, org_id=org, task_id=a["id"], other_id=c["id"])

    # org-wide branch (no task_id): all edges, then a bounded slice.
    assert len(await list_dependencies(token=token, org_id=org)) == 2
    assert len(await list_dependencies(token=token, org_id=org, limit=1)) == 1
    assert len(await list_task_relations(token=token, org_id=org)) == 2
    assert len(await list_task_relations(token=token, org_id=org, limit=1)) == 1


async def test_list_running_timers_defaults_to_caller() -> None:
    token, org, _oid, _uid = await _signup("disc-timers")
    task = await create_task(token=token, org_id=org, title="tracked")
    await start_timer(token=token, org_id=org, task_id=task["id"])
    # No user_id / handle: the caller's own running timers.
    mine = await list_running_timers(token=token, org_id=org)
    assert len(mine) == 1
    # An unknown handle resolves to nobody -> empty, never an error.
    assert (
        await list_running_timers(token=token, org_id=org, handle=f"ghost-{uuid.uuid4().hex}") == []
    )
