"""``GET /activity/watermark``: the count a live view polls.

Four properties, and the second is the one the endpoint exists for:

- a bootstrap answers with the instant the server chose, so the caller never
  has to trust its own clock against the database's;
- a change made through a path that does NOT touch ``tasks.updated_at`` --
  attaching a tag writes a junction row and an audit row and leaves the task
  row alone -- still moves the count. This is why the probe reads the audit
  log and not the task table;
- what happens in another workspace is invisible, the same way every other
  tenant-scoped read is;
- a scope the server does not know is refused rather than answered from the
  whole log.

Observed failing against a watermark keyed on ``tasks.updated_at``
(2026-09-19): the tag case read the same count before and after the attach
(1 and 1), which is the silent half of the bug -- the view would simply
never refresh for the edits an agent makes most.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> dict[str, str]:
    a = (await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})).json()
    return {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}


async def _watermark(c: AsyncClient, h: dict[str, str], since: str | None = None) -> dict[str, str]:
    params: dict[str, str] = {"scope": "tasks"}
    if since is not None:
        params["since"] = since
    r = await c.get("/activity/watermark", headers=h, params=params)
    assert r.status_code == 200, r.text
    return dict(r.json())


async def test_bootstrap_returns_the_servers_own_instant() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        boot = await _watermark(c, h)
        assert boot["since"]
        # Handing the instant straight back must be stable: nothing has
        # happened in between, so the count cannot move on its own.
        again = await _watermark(c, h, since=str(boot["since"]))
        assert again["changes"] == (await _watermark(c, h, since=str(boot["since"])))["changes"]


async def test_a_tag_attached_to_a_task_moves_the_count() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        task = (await c.post("/tasks", headers=h, json={"title": "watched"})).json()
        tag = (
            await c.post(
                "/tags", headers=h, json={"kind": "generic", "name": f"t-{uuid.uuid4().hex[:6]}"}
            )
        ).json()

        boot = await _watermark(c, h)
        since = str(boot["since"])
        base = boot["changes"]

        r = await c.post(f"/tasks/{task['id']}/tags", headers=h, json={"tag_id": tag["id"]})
        assert r.status_code == 204, r.text

        after = await _watermark(c, h, since=since)
        assert after["changes"] > base
        # The instant is echoed unchanged: the caller keeps polling from the
        # same place until it decides to re-read.
        assert after["since"] == since


async def test_another_workspace_is_invisible() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        watcher = await _signup(c)
        stranger = await _signup(c)

        boot = await _watermark(c, watcher)
        since, base = str(boot["since"]), boot["changes"]

        r = await c.post("/tasks", headers=stranger, json={"title": "not yours"})
        assert r.status_code == 200, r.text

        assert (await _watermark(c, watcher, since=since))["changes"] == base


async def test_an_unknown_scope_is_refused() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        r = await c.get("/activity/watermark", headers=h, params={"scope": "everything"})
        assert r.status_code == 422
