"""The read path a thin client actually needs from ``GET /tasks``.

The service has accepted ``q``, ``open_only``, the date windows, an
ordering and a ``limit`` for a long time; MCP uses all of them. This
route exposed none, so an HTTP caller wanting "the five most overdue" had
to download every task in the workspace and cut the list itself:
unbounded transfer to render five rows, plus a second implementation of
an ordering the database already does. That is the asymmetry these cover,
along with ``ids``, which turns hydrating a page of search hits from N
requests into one.

Also here: the ordering vocabulary is a closed set at the boundary. The
service answers an unrecognised sort key by falling back to its default,
which is right in-process and wrong over HTTP, because a typo then looks
exactly like a sort that was applied.
"""

from __future__ import annotations

import typing
import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app
from mycelium_api.schemas import TaskOrderBy
from mycelium_core.services.tasks import _TASK_ORDER


def test_the_sort_vocabulary_is_one_vocabulary() -> None:
    """Both directions. A key the service knows and the route does not
    offer is unreachable; a key the route offers and the service has
    dropped stops sorting without failing."""
    assert set(typing.get_args(TaskOrderBy)) == set(_TASK_ORDER)


async def _owner(c: AsyncClient) -> dict[str, str]:
    su = (
        await c.post(
            "/auth/signup",
            json={
                "email": f"{uuid.uuid4().hex[:10]}@example.test",
                "password": "pw-strong-123",
                "workspace_name": "READ",
            },
        )
    ).json()
    return {
        "Authorization": f"Bearer {su['token']}",
        "X-Workspace-Id": su["workspace_id"],
        "X-Workspace-Role": "owner",
    }


async def _task(c: AsyncClient, h: dict[str, str], title: str, **extra: object) -> dict:
    res = await c.post("/tasks", headers=h, json={"title": title, **extra})
    assert res.status_code == 200, res.text
    return res.json()


async def test_ids_returns_exactly_those_rows() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        a = await _task(c, h, "alpha")
        b = await _task(c, h, "bravo")
        await _task(c, h, "charlie")

        res = await c.get("/tasks", headers=h, params={"ids": [a["id"], b["id"]]})
        assert res.status_code == 200, res.text
        assert {r["id"] for r in res.json()} == {a["id"], b["id"]}


async def test_ids_ignores_an_id_from_another_workspace() -> None:
    """Not a filter question, a tenancy one: an id is only unique inside
    a workspace, and a client that pastes one from elsewhere must get
    nothing rather than a leak."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        mine = await _owner(c)
        theirs = await _owner(c)
        other = await _task(c, theirs, "not yours")

        res = await c.get("/tasks", headers=mine, params={"ids": [other["id"]]})
        assert res.status_code == 200
        assert res.json() == []


async def test_limit_and_order_return_the_top_of_the_list_not_a_slice_of_everything() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        # priority is importance x urgency and 1 is the MOST pressing
        # (ADR-0004), so the default ascending order puts the critical
        # task first. The client never computes this: it asks for an
        # order and a count, and the server decides what comes first.
        most = await _task(c, h, "critical now", importance=1, urgency=1)
        least = await _task(c, h, "trivial whenever", importance=5, urgency=5)
        await _task(c, h, "middling", importance=3, urgency=3)

        res = await c.get("/tasks", headers=h, params={"order_by": "priority", "limit": 1})
        assert res.status_code == 200, res.text
        rows = res.json()
        assert len(rows) == 1
        assert rows[0]["id"] == most["id"]

        desc = await c.get(
            "/tasks",
            headers=h,
            params={"order_by": "priority", "order_desc": "true", "limit": 1},
        )
        assert desc.json()[0]["id"] == least["id"]


async def test_an_unknown_sort_key_is_refused_rather_than_ignored() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        res = await c.get("/tasks", headers=h, params={"order_by": "titel"})
        assert res.status_code == 422


async def test_q_narrows_server_side() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        wanted = await _task(c, h, "preventivo Bianchi")
        await _task(c, h, "chiamare il fornaio")

        res = await c.get("/tasks", headers=h, params={"q": "preventivo"})
        assert [r["id"] for r in res.json()] == [wanted["id"]]


async def test_projects_picker_narrows_and_caps() -> None:
    """``GET /clients`` has had q/limit/recent since it was written and
    ``GET /projects`` had none, so a project picker could only fetch the
    lot and match locally -- the same match, implemented twice, over a
    list nothing bounds."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        created = await c.post(
            "/clients", headers=h, json={"name": "Acme", "legal_name": "Acme Ltd"}
        )
        assert created.status_code in (200, 201), created.text
        client = created.json()
        for name in ("Website", "Warehouse", "Wiring"):
            res = await c.post(
                "/projects", headers=h, json={"name": name, "client_tag_id": client["id"]}
            )
            assert res.status_code in (200, 201), res.text

        narrowed = await c.get("/projects", headers=h, params={"q": "web"})
        assert [p["name"] for p in narrowed.json()] == ["Website"]

        capped = await c.get("/projects", headers=h, params={"limit": 2})
        assert len(capped.json()) == 2

        by_client = await c.get("/projects", headers=h, params={"client_tag_id": client["id"]})
        assert len(by_client.json()) == 3
