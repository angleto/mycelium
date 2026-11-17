"""The CLI's token is an agent's, and the CLI's own code is enough for it.

``mycelium auth login`` mints an agent token (scope ``cli``, no assistant)
and stores it in a file every process of the user can read. Over REST it is
published as ``human_api`` with the token as subject, which is an agent to
``services.caller`` (docs/adr/0072). Before that, the same token was a
person: it moved tasks it never took, took the owner's exemption and could
rewrite a workflow, and so could every agent on the machine that read the
file.

The moves here run the CLI's real ``tasks._transition`` against the real
app: a synchronous httpx transport hands each request to the ASGI client on
the test's loop. A stub would only repeat what the CLI assumes about the
routes, which is how the first version called ``/workers`` instead of
``/tasks/workers`` with its stubbed tests green.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text

from mycelium_api.main import app
from mycelium_cli.cmds import tasks as tasks_cmd
from mycelium_cli.http import CLIError
from mycelium_core.config import get_settings


class _Bridge(httpx.BaseTransport):
    """The CLI's synchronous client, served by the app on the test's loop.

    The CLI runs in a worker thread (``asyncio.to_thread``), so blocking on
    the loop's future here does not block the loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop, ac: AsyncClient) -> None:
        self.loop = loop
        self.ac = ac

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        req = self.ac.build_request(
            request.method,
            str(request.url),
            headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
            content=request.read(),
        )
        r = asyncio.run_coroutine_threadsafe(self.ac.send(req), self.loop).result()
        return httpx.Response(r.status_code, headers=r.headers, content=r.content)


async def _owner(c: AsyncClient) -> dict[str, str]:
    su = (
        await c.post(
            "/auth/signup",
            json={
                "email": f"{uuid.uuid4().hex[:10]}@example.test",
                "password": "pw-strong-123",
                "workspace_name": "CLI",
            },
        )
    ).json()
    return {
        "Authorization": f"Bearer {su['token']}",
        "X-Workspace-Id": su["workspace_id"],
        "X-Workspace-Role": "owner",
    }


async def _cli_token(c: AsyncClient, owner: dict[str, str]) -> dict[str, str]:
    """What ``mycelium auth login`` mints and the CLI then sends."""
    r = await c.post("/agent-tokens", headers=owner, json={"name": "cli", "scope": "cli"})
    assert r.status_code in (200, 201), r.text
    return {
        "Authorization": f"Bearer {r.json()['raw']}",
        "X-Workspace-Id": owner["X-Workspace-Id"],
    }


async def _worker_open(c: AsyncClient, h: dict[str, str]) -> str:
    r = await c.post("/tasks/workers", headers=h, json={"label": "terminal"})
    assert r.status_code == 200, r.text
    return str(r.json()["id"])


def _as_cli(monkeypatch: pytest.MonkeyPatch, ac: AsyncClient, h: dict[str, str]) -> None:
    loop = asyncio.get_running_loop()

    @contextlib.contextmanager
    def _client(*_a: Any, **_k: Any):
        with httpx.Client(transport=_Bridge(loop, ac), base_url="http://t", headers=h) as sc:
            yield sc

    monkeypatch.setattr(tasks_cmd, "client", _client)


async def _move(task_id: str, state: str, worker: str | None) -> str:
    """``mycelium task to <task> <state> [--worker W]``: "ok", or the error."""
    try:
        await asyncio.to_thread(tasks_cmd._transition, task_id, target_name=state, worker=worker)
    except CLIError as exc:
        return str(exc)
    return "ok"


async def _held(c: AsyncClient, h: dict[str, str], task_id: str) -> int:
    return len((await c.get(f"/tasks/{task_id}/leases", headers=h)).json())


async def _task(c: AsyncClient, h: dict[str, str]) -> str:
    return str((await c.post("/tasks", headers=h, json={"title": "t"})).json()["id"])


# The new workspace's Default workflow is todo -> in_progress -> done, so
# in_progress is both where the work is done and where it is handed over.


async def test_without_a_worker_the_server_refuses_the_cli_token() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        cli = await _cli_token(c, await _owner(c))
        tid = await _task(c, cli)
        states = (await c.get(f"/tasks/{tid}/states", headers=cli)).json()
        doing = next(s for s in states if s["name"] == "in_progress")
        r = await c.post(
            f"/tasks/{tid}/state",
            headers=cli,
            json={"state_id": doing["id"], "expected_version": 1},
        )
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "task.lease.required"


async def test_a_cli_worker_moves_its_work_and_another_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        cli = await _cli_token(c, await _owner(c))
        _as_cli(monkeypatch, c, cli)
        author = await _worker_open(c, cli)
        checker = await _worker_open(c, cli)
        tid = await _task(c, cli)
        assert await _move(tid, "in_progress", author) == "ok"
        assert await _held(c, cli, tid) == 0
        assert "task.lease.self_verification" in await _move(tid, "done", author)
        # The refused close gave back the hold it took to try.
        assert await _held(c, cli, tid) == 0
        assert await _move(tid, "done", checker) == "ok"


async def test_an_expired_hold_of_the_same_worker_is_taken_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreleased lease past its deadline is still listed until the sweep;
    reading it as held made every move fail until then."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        cli = await _cli_token(c, await _owner(c))
        _as_cli(monkeypatch, c, cli)
        me = await _worker_open(c, cli)
        tid = await _task(c, cli)
        r = await c.post(f"/tasks/{tid}/leases", headers=cli, json={"worker_id": me})
        assert r.status_code == 200, r.text
        eng = create_engine(get_settings().database_url_sync)
        try:
            with eng.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE task_leases SET expires_at = now() - interval '5 minutes' "
                        "WHERE task_id = :t AND released_at IS NULL"
                    ),
                    {"t": tid},
                )
        finally:
            eng.dispose()
        assert await _move(tid, "in_progress", me) == "ok"


async def test_a_hold_taken_before_survives_a_refused_move_whatever_the_case_of_the_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        cli = await _cli_token(c, await _owner(c))
        _as_cli(monkeypatch, c, cli)
        me = await _worker_open(c, cli)
        tid = await _task(c, cli)
        await c.post(f"/tasks/{tid}/leases", headers=cli, json={"worker_id": me})
        # todo -> done has no check behind it: refused.
        assert await _move(tid, "done", me.upper()) != "ok"
        assert await _held(c, cli, tid) == 1


async def test_a_closed_worker_is_named_with_the_shell_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        cli = await _cli_token(c, await _owner(c))
        _as_cli(monkeypatch, c, cli)
        gone = await _worker_open(c, cli)
        await c.post(f"/tasks/workers/{gone}/close", headers=cli)
        tid = await _task(c, cli)
        with pytest.raises(CLIError) as exc:
            await asyncio.to_thread(
                tasks_cmd._transition, tid, target_name="in_progress", worker=gone
            )
        assert "worker open" in (exc.value.hint or "")
        assert await _held(c, cli, tid) == 0


async def test_the_cli_token_cannot_write_a_workflow() -> None:
    doc = {
        "kind": "mycelium.workflow",
        "version": 1,
        "name": f"imp-{uuid.uuid4().hex[:6]}",
        "states": [
            {"name": "a", "is_initial": True},
            {"name": "b", "is_terminal": True},
        ],
        "transitions": [{"from_state": "a", "to_state": "b"}],
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        owner = await _owner(c)
        r = await c.post("/workflows/import", headers=owner, json=doc)
        assert r.status_code in (200, 201), r.text
        cli = await _cli_token(c, owner)
        doc["name"] = f"imp-{uuid.uuid4().hex[:6]}"
        r = await c.post(
            "/workflows/import", headers={**cli, "X-Workspace-Role": "owner"}, json=doc
        )
        assert r.status_code == 403, r.text
