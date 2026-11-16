"""The work digest: what happened on a client's or project's tasks in a window.

What it promises, each asserted against the running API: a task whose only
activity in the window is a comment is listed (the case ``updated_since``
misses), the scope is the client or project and nothing else, the window is
half-open on the instant the time entry STARTED, the time entry carries its
memo and only completed entries count, a state change names both states, the
bin is left out, a section the credential may not read is withheld and never
leaks a title, and the MCP tool is the same operation.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app

UTC = dt.UTC


async def _owner(c: AsyncClient) -> dict[str, str]:
    su = (
        await c.post(
            "/auth/signup",
            json={
                "email": f"{uuid.uuid4().hex[:10]}@example.test",
                "password": "pw-strong-123",
                "workspace_name": "DIGEST",
            },
        )
    ).json()
    return {
        "Authorization": f"Bearer {su['token']}",
        "X-Workspace-Id": su["workspace_id"],
        "X-Workspace-Role": "owner",
    }


async def _client_and_project(c: AsyncClient, h: dict[str, str], name: str) -> tuple[str, str]:
    cli = (
        await c.post("/clients", headers=h, json={"name": name, "legal_name": f"{name} srl"})
    ).json()
    proj = (
        await c.post("/projects", headers=h, json={"name": f"{name} P", "client_tag_id": cli["id"]})
    ).json()
    return cli["id"], proj["id"]


async def _task(c: AsyncClient, h: dict[str, str], title: str, project: str, **kw: Any) -> dict:
    r = await c.post("/tasks", headers=h, json={"title": title, "tag_ids": [project], **kw})
    assert r.status_code == 200, r.text
    return r.json()


async def _comment(c: AsyncClient, h: dict[str, str], task_id: str, body: str) -> dict:
    r = await c.post(f"/tasks/{task_id}/comments", headers=h, json={"body": body})
    assert r.status_code == 200, r.text
    return r.json()


async def _entry(
    c: AsyncClient, h: dict[str, str], task_id: str, started: dt.datetime, minutes: int, memo: str
) -> dict:
    r = await c.post(
        "/time/entries",
        headers=h,
        json={
            "task_id": task_id,
            "started_at": started.isoformat(),
            "ended_at": (started + dt.timedelta(minutes=minutes)).isoformat(),
            "memo": memo,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _window(start: dt.datetime, end: dt.datetime, **kw: str) -> dict[str, str]:
    return {"start_from": start.isoformat(), "start_to": end.isoformat(), **kw}


async def _digest(c: AsyncClient, h: dict[str, str], **params: str) -> dict:
    r = await c.get("/tasks/digest", headers=h, params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def _now() -> dt.datetime:
    """The database's clock, not this process's: every instant the digest
    compares is stamped by Postgres, and a container's clock can sit apart
    from the host's by more than the gap between two requests."""
    from sqlalchemy import text

    from mycelium_core.db import admin_session

    async with admin_session() as s:
        return (await s.execute(text("SELECT clock_timestamp()"))).scalar_one()


async def test_a_task_with_only_a_comment_in_the_window_is_listed() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        cli, proj = await _client_and_project(c, h, "Acme")
        t = await _task(c, h, "only commented", proj)
        # Created well before the window would be the real case; here the
        # window starts after creation, so the comment is the only event.
        start = await _now()
        await _comment(c, h, t["id"], "talked to the client, schema agreed")
        end = await _now()
        await _comment(c, h, t["id"], "written after the window closed")
        d = await _digest(c, h, **_window(start, end, client_tag_id=cli))

        assert [x["task_id"] for x in d["tasks"]] == [t["id"]]
        events = d["tasks"][0]["events"]
        assert [e["kind"] for e in events] == ["comment"]
        assert events[0]["text"] == "talked to the client, schema agreed"
        assert events[0]["actor"]
        assert d["withheld"] == []

        # And a window that closes before both comments has neither: only the
        # creation of the task, which is what happened in it.
        d = await _digest(c, h, **_window(start - dt.timedelta(hours=1), start, client_tag_id=cli))
        assert [e["kind"] for e in d["tasks"][0]["events"]] == ["task_created"]


async def test_the_scope_is_the_client_and_the_bin_is_left_out() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        cli_a, proj_a = await _client_and_project(c, h, "A")
        _, proj_b = await _client_and_project(c, h, "B")
        start = await _now()
        mine = await _task(c, h, "client A work", proj_a)
        await _task(c, h, "client B work", proj_b)
        binned = await _task(c, h, "thrown away", proj_a)
        await _comment(c, h, binned["id"], "never mind")
        r = await c.post(
            f"/tasks/{binned['id']}/delete",
            headers=h,
            json={"expected_version": binned["version"]},
        )
        assert r.status_code == 200, r.text
        end = await _now() + dt.timedelta(minutes=5)

        d = await _digest(c, h, **_window(start, end, client_tag_id=cli_a))
        assert [x["task_id"] for x in d["tasks"]] == [mine["id"]]
        assert d["tasks"][0]["client_name"] == "A"
        assert d["tasks"][0]["events"][0]["kind"] == "task_created"
        assert d["tasks"][0]["description"] is None

        d = await _digest(c, h, **_window(start, end, project_tag_id=proj_b))
        assert [x["title"] for x in d["tasks"]] == ["client B work"]


async def test_time_entries_by_start_with_memo_and_only_completed_ones_counted() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        cli, proj = await _client_and_project(c, h, "Time")
        t = await _task(c, h, "tracked", proj)
        week = dt.datetime(2026, 11, 2, tzinfo=UTC)
        await _entry(c, h, t["id"], week + dt.timedelta(days=1), 90, "migrated the ingest schema")
        # Started one second before the window: out, whatever its end.
        await _entry(c, h, t["id"], week - dt.timedelta(seconds=1), 30, "before")
        # Started exactly at the exclusive end: out.
        await _entry(c, h, t["id"], week + dt.timedelta(days=7), 30, "after")
        r = await c.post("/time/start", headers=h, json={"task_id": t["id"]})
        assert r.status_code == 200, r.text

        d = await _digest(c, h, **_window(week, week + dt.timedelta(days=7), client_tag_id=cli))
        (task,) = d["tasks"]
        entries = [e for e in task["events"] if e["kind"] == "time_entry"]
        assert [e["text"] for e in entries] == ["migrated the ingest schema"]
        assert entries[0]["duration_seconds"] == 90 * 60
        assert task["seconds"] == 90 * 60 == d["seconds"]
        assert task["billable_seconds"] == d["billable_seconds"]

        # The running timer is in today's window, listed and not counted.
        d = await _digest(
            c,
            h,
            **_window(await _now() - dt.timedelta(hours=1), await _now() + dt.timedelta(hours=1)),
            client_tag_id=cli,
        )
        running = [e for e in d["tasks"][0]["events"] if e["kind"] == "time_entry"]
        assert [e["running"] for e in running] == [True]
        assert running[0]["duration_seconds"] is None
        assert d["seconds"] == 0


async def test_a_state_change_names_both_states() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        cli, proj = await _client_and_project(c, h, "States")
        t = await _task(c, h, "moved", proj)
        states = {
            s["name"]: s["id"] for s in (await c.get(f"/tasks/{t['id']}/states", headers=h)).json()
        }
        start = await _now()
        r = await c.post(
            f"/tasks/{t['id']}/state",
            headers=h,
            json={"expected_version": t["version"], "state_id": states["in_progress"]},
        )
        assert r.status_code == 200, r.text
        d = await _digest(
            c, h, **_window(start, await _now() + dt.timedelta(minutes=5), client_tag_id=cli)
        )
        (ev,) = d["tasks"][0]["events"]
        assert ev["kind"] == "state_changed"
        assert (ev["from_state"], ev["to_state"]) == ("todo", "in_progress")
        assert d["tasks"][0]["state"] == "in_progress"


async def test_consecutive_description_edits_fold_into_one() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        cli, proj = await _client_and_project(c, h, "Desc")
        t = await _task(c, h, "described", proj)
        start = await _now()
        version = t["version"]
        for text in ("one", "one two", "one two three"):
            r = await c.patch(
                f"/tasks/{t['id']}",
                headers=h,
                json={"expected_version": version, "description": text},
            )
            assert r.status_code == 200, r.text
            version = r.json()["version"]
        await _comment(c, h, t["id"], "between")
        r = await c.patch(
            f"/tasks/{t['id']}",
            headers=h,
            json={"expected_version": version, "description": "final"},
        )
        assert r.status_code == 200, r.text
        d = await _digest(
            c, h, **_window(start, await _now() + dt.timedelta(minutes=5), client_tag_id=cli)
        )
        events = d["tasks"][0]["events"]
        assert [(e["kind"], e["count"]) for e in events] == [
            ("description_edited", 3),
            ("comment", 1),
            ("description_edited", 1),
        ]
        assert d["tasks"][0]["description"] == "final"


async def test_the_window_is_validated() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        now = await _now()
        cases = [
            # Naive: where a day starts is the caller's to say.
            {"start_from": "2026-11-09T00:00:00", "start_to": "2026-11-16T00:00:00"},
            _window(now, now),
            _window(now, now - dt.timedelta(days=1)),
            _window(now, now + dt.timedelta(days=93)),
        ]
        for params in cases:
            r = await c.get("/tasks/digest", headers=h, params=params)
            assert r.status_code == 422, (params, r.text)
        r = await c.get(
            "/tasks/digest", headers=h, params=_window(now, now + dt.timedelta(days=92))
        )
        assert r.status_code == 200, r.text


async def _assistant(c: AsyncClient, owner: dict[str, str], scope: list[str]) -> dict[str, str]:
    created = (
        await c.post(
            "/ai-assistants",
            headers=owner,
            json={"label": "Ext", "handle": f"e{uuid.uuid4().hex[:8]}", "scope": scope},
        )
    ).json()
    return {
        "Authorization": f"Bearer {created['raw_secret']}",
        "X-Workspace-Id": owner["X-Workspace-Id"],
    }


async def test_a_section_the_credential_may_not_read_is_withheld() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        cli, proj = await _client_and_project(c, h, "Scoped")
        start = await _now()
        timed = await _task(c, h, "has time", proj, description="internal notes")
        commented = await _task(c, h, "has only a comment", proj)
        await _entry(c, h, timed["id"], start, 15, "memo seen by time:read")
        await _comment(c, h, commented["id"], "comment not for time:read")
        params = _window(start - dt.timedelta(minutes=1), await _now() + dt.timedelta(minutes=5))

        time_only = await _assistant(c, h, ["time:read"])
        d = await _digest(c, time_only, **params, client_tag_id=cli)
        assert d["withheld"] == ["comments:read", "tasks:read"]
        # The task with only a comment is not listed, so its title does not
        # reach a credential that could not have read the comment.
        assert [x["task_id"] for x in d["tasks"]] == [timed["id"]]
        (task,) = d["tasks"]
        assert [e["kind"] for e in task["events"]] == ["time_entry"]
        assert task["description"] is None and task["state"] is None
        assert "comment not for time:read" not in str(d)
        assert "internal notes" not in str(d)

        comments_only = await _assistant(c, h, ["comments:read"])
        d = await _digest(c, comments_only, **params, client_tag_id=cli)
        assert [x["task_id"] for x in d["tasks"]] == [commented["id"]]
        assert d["seconds"] == 0
        assert "memo seen by time:read" not in str(d)

        # A credential with none of the three is refused at the gate.
        neither = await _assistant(c, h, ["notes:read"])
        r = await c.get("/tasks/digest", headers=neither, params=params)
        assert r.status_code == 403, r.text


async def test_another_workspace_sees_nothing() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        cli, proj = await _client_and_project(c, h, "Mine")
        start = await _now()
        t = await _task(c, h, "private", proj)
        await _comment(c, h, t["id"], "secret")
        other = await _owner(c)
        d = await _digest(
            c, other, **_window(start, await _now() + dt.timedelta(minutes=5), client_tag_id=cli)
        )
        assert d["tasks"] == []


async def test_the_mcp_tool_is_the_same_operation_and_honours_the_scope() -> None:
    from mycelium_core.db import admin_session
    from mycelium_core.services.auth import signup
    from mycelium_mcp import tools_tasks
    from mycelium_mcp.server import _PRINCIPAL_SCOPE, list_time_entries

    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP DIGEST",
        )
    token, org = r.token, str(r.org_id)
    assert token is not None
    h = {"Authorization": f"Bearer {token}", "X-Workspace-Id": org, "X-Workspace-Role": "owner"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        cli, proj = await _client_and_project(c, h, "Mcp")
        start = await _now()
        t = await _task(c, h, "over mcp", proj)
        await _comment(c, h, t["id"], "a comment")
        await _entry(c, h, t["id"], start, 10, "a memo")
    params = _window(start - dt.timedelta(minutes=1), await _now() + dt.timedelta(minutes=5))

    d = await tools_tasks.TOOLS["work_digest"](token=token, org_id=org, client_tag_id=cli, **params)
    kinds = sorted(e["kind"] for e in d["tasks"][0]["events"])
    assert kinds == ["comment", "task_created", "time_entry"]
    assert d["withheld"] == []

    reset = _PRINCIPAL_SCOPE.set(["time:read"])
    try:
        d = await tools_tasks.TOOLS["work_digest"](
            token=token, org_id=org, client_tag_id=cli, **params
        )
    finally:
        _PRINCIPAL_SCOPE.reset(reset)
    assert d["withheld"] == ["comments:read", "tasks:read"]
    assert [e["kind"] for e in d["tasks"][0]["events"]] == ["time_entry"]

    # The memo is readable over MCP, filtered by the window and the client.
    rows = await list_time_entries(token=token, org_id=org, client_tag_id=cli, **params)
    assert [e["memo"] for e in rows] == ["a memo"]
    rows = await list_time_entries(
        token=token,
        org_id=org,
        client_tag_id=cli,
        start_from=(start + dt.timedelta(hours=1)).isoformat(),
        start_to=(start + dt.timedelta(hours=2)).isoformat(),
    )
    assert rows == []


async def test_an_accepted_suggestion_is_a_description_edit_not_a_comment() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        cli, proj = await _client_and_project(c, h, "Sugg")
        t = await _task(c, h, "suggested", proj, description="ship on friday")
        start = await _now()
        r = await c.post(
            "/annotations/suggestion",
            headers=h,
            json={
                "doc_kind": "task_description",
                "doc_id": t["id"],
                "original_text": "friday",
                "proposed_text": "friday 30 October",
            },
        )
        assert r.status_code == 200, r.text
        sugg = r.json()
        r = await c.post(
            f"/annotations/{sugg['id']}/accept",
            headers=h,
            json={"expected_version": sugg["version"]},
        )
        assert r.status_code == 200, r.text
        d = await _digest(
            c, h, **_window(start, await _now() + dt.timedelta(minutes=5), client_tag_id=cli)
        )
        assert [e["kind"] for e in d["tasks"][0]["events"]] == ["description_edited"]
        assert d["tasks"][0]["description"] == "ship on friday 30 October"
