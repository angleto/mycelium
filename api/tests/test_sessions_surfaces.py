"""The sessions a person steers, through the surfaces that reach them
(docs/adr/0078): the routes, the MCP tools, and the CLI's hook driving the
real application.

The service is tested against Postgres in core/tests/test_agent_sessions.py.
What is pinned here is what only the surfaces can get wrong: that
``worker_open`` takes the join key on BOTH surfaces (its route and its tool
are written by hand, and nothing else holds them together), that a person's
browser cannot open a session, that the read reaches MCP, and that the
hook's calls are the ones the server answers.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


class _Bridge(httpx.BaseTransport):
    """The CLI's synchronous client served by the app on the test's loop;
    the CLI runs in a worker thread, so blocking here does not block it."""

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
                "workspace_name": "SESSIONS",
            },
        )
    ).json()
    return {
        "Authorization": f"Bearer {su['token']}",
        "X-Workspace-Id": su["workspace_id"],
        "X-Workspace-Role": "owner",
    }


async def _token(c: AsyncClient, owner: dict[str, str], scope: str) -> tuple[dict[str, str], str]:
    r = await c.post("/agent-tokens", headers=owner, json={"name": scope, "scope": scope})
    assert r.status_code in (200, 201), r.text
    body = r.json()
    return (
        {"Authorization": f"Bearer {body['raw']}", "X-Workspace-Id": owner["X-Workspace-Id"]},
        str(body.get("id") or body["token"]["id"]),
    )


async def _open(c: AsyncClient, h: dict[str, str], ref: str) -> dict[str, Any]:
    r = await c.post(
        "/sessions/open",
        headers=h,
        json={"agent": "claude_code", "host_ref": ref, "project": "humus", "machine": "lab"},
    )
    assert r.status_code == 200, r.text
    return dict(r.json())


async def test_the_rest_worker_joins_by_key_and_the_page_shows_it() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        owner = await _owner(c)
        hooks, _ = await _token(c, owner, "cli")
        agent, _ = await _token(c, owner, "cli")
        opened = await _open(c, hooks, "cc-rest")

        w = await c.post(
            "/tasks/workers", headers=agent, json={"label": "cc", "join_key": opened["join_key"]}
        )
        assert w.status_code == 200, w.text
        assert w.json()["session_id"] == opened["session"]["id"]

        stale = await c.post("/tasks/workers", headers=agent, json={"join_key": "nope"})
        assert stale.status_code == 404
        assert stale.json()["code"] == "session.join_key_unknown"

        page = (await c.get("/sessions", headers=owner)).json()
        (row,) = page["sessions"]
        assert row["session_id"] == opened["session"]["id"]
        assert row["worker_ids"] == [w.json()["id"]]
        assert row["group"] == "needs_you"
        assert page["counts"] == {"needs_you": 1, "working": 0, "quiet": 0}


async def test_a_persons_browser_cannot_open_or_report_a_session() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        owner = await _owner(c)
        r = await c.post(
            "/sessions/open", headers=owner, json={"agent": "claude_code", "host_ref": "x"}
        )
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "session.needs_credential"


async def test_the_mcp_worker_joins_by_key_and_the_read_reaches_mcp() -> None:
    from mycelium_mcp import server as mcp_server
    from mycelium_mcp import tools_sessions

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        owner = await _owner(c)
        hooks, _ = await _token(c, owner, "cli")
        _, agent_token_id = await _token(c, owner, "mcp")
        opened = await _open(c, hooks, "cc-mcp")
        me = (await c.get("/auth/me", headers=owner)).json()

    org = uuid.UUID(owner["X-Workspace-Id"])
    user = uuid.UUID(str(me["user_id"]))
    reset = mcp_server._PRINCIPAL.set((user, org, uuid.UUID(agent_token_id)))
    try:
        w = await mcp_server.worker_open(
            token="", org_id="", label="cc", join_key=opened["join_key"]
        )
        page = await tools_sessions.TOOLS["sessions_list"](token="", org_id="")
    finally:
        mcp_server._PRINCIPAL.reset(reset)

    assert w["session_id"] == opened["session"]["id"]
    rows = page["sessions"] if isinstance(page, dict) else json.loads(page)["sessions"]
    assert [r["session_id"] for r in rows] == [opened["session"]["id"]]


@pytest.fixture
def _state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("MYCELIUM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MYCELIUM_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("MYCELIUM_MACHINE_LABEL", "lab")
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(tmp_path / "env"))
    yield tmp_path


async def test_the_hook_drives_the_real_server_through_a_turn(
    monkeypatch: pytest.MonkeyPatch, _state: Path
) -> None:
    """The CLI's hook, unchanged, against the application: what it sends is
    what the server accepts, and what it reads back is what it sent."""
    from mycelium_cli import session_hook
    from mycelium_cli import session_sync as sync

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        owner = await _owner(c)
        hooks, _ = await _token(c, owner, "cli")
        loop = asyncio.get_running_loop()

        @contextlib.contextmanager
        def bridged(*_a: Any, **_k: Any) -> Iterator[httpx.Client]:
            with httpx.Client(transport=_Bridge(loop, c), base_url="http://t", headers=hooks) as sc:
                yield sc

        monkeypatch.setattr(sync, "client", bridged)
        monkeypatch.setattr(sync, "logged_in", lambda: True)
        monkeypatch.setattr(session_hook, "_detached", lambda _mode: None)
        # The registry is this machine's; in a test it lists the one session.
        live = sync.agent_host.LiveSession(
            "cc-hook", None, str(_state), "interactive", "busy", None, None
        )  # one literal row
        monkeypatch.setattr(sync.agent_host, "live_sessions", lambda: [live])

        def run(payload: dict[str, Any]) -> str:
            out = io.StringIO()
            monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
            monkeypatch.setattr("sys.stdout", out)
            assert session_hook.main([]) == 0
            return out.getvalue()

        base = {"session_id": "cc-hook", "cwd": str(_state)}
        started = await asyncio.to_thread(run, {"hook_event_name": "SessionStart", **base})
        key = (_state / "env").read_text(encoding="utf-8").strip().split("=", 1)[1]
        assert f"join_key='{key}'" in json.loads(started)["hookSpecificOutput"]["additionalContext"]

        await asyncio.to_thread(
            run,
            {"hook_event_name": "Notification", "notification_type": "permission_prompt", **base},
        )
        (row,) = (await c.get("/sessions", headers=owner)).json()["sessions"]
        assert (row["attention"], row["blocked_on"], row["machine"]) == (
            "blocked",
            "permission",
            "lab",
        )

        await asyncio.to_thread(run, {"hook_event_name": "Stop", **base})
        (row,) = (await c.get("/sessions", headers=owner)).json()["sessions"]
        assert row["attention"] == "your_turn"

        await asyncio.to_thread(run, {"hook_event_name": "SessionEnd", **base})
        assert (await c.get("/sessions", headers=owner)).json()["sessions"] == []

    log = _state / "state" / "hooks.log"
    assert not log.exists(), log.read_text(encoding="utf-8")
