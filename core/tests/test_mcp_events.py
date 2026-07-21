"""MCP ``list_events``: read the coordination bus over MCP (enabler C, ADR-0036).

The event bus (``event_bus`` service, migration 0049) already backs the REST
``GET /garden/audit`` audit panel. Enabler C exposes the SAME read stream on
MCP so an agent can OBSERVE what its human / agent collaborators are doing by
polling (the streaming subscription is deferred by ADR-0036). Gated on the new
``events:read`` scope, consistent with the REST route.
"""

from __future__ import annotations

import uuid
from typing import Literal

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.mcp_scopes import DEFAULT_SCOPES, VALID_SCOPE_KEYS
from mycelium_core.services import agent_tokens as at_svc
from mycelium_core.services import event_bus
from mycelium_core.services.auth import signup
from mycelium_mcp.server import _PRINCIPAL_SCOPE, _scope_permits, list_events


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _workspace() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="EVMCP")
    return a.org_id, a.user_id


async def _emit(
    org: uuid.UUID,
    user: uuid.UUID,
    kind: Literal["read", "propose", "commit", "reject", "snapshot"],
    n: int,
):  # type: ignore[no-untyped-def]
    async with tenant_session(str(org), str(user)) as s:
        return await event_bus.emit_event(
            s,
            org_id=org,
            actor_id=user,
            actor_kind="human",
            kind=kind,
            payload={"n": n},
            node_kind="note",
            node_id=uuid.uuid4(),
        )


async def _token(org: uuid.UUID, user: uuid.UUID) -> str:
    async with tenant_session(str(org), str(user)) as s:
        m = await at_svc.mint(s, org_id=org, actor_id=user, name="observer")
    return m.raw


async def test_list_events_returns_bus_events_newest_first() -> None:
    org, user = await _workspace()
    e1 = await _emit(org, user, "snapshot", 1)
    e2 = await _emit(org, user, "commit", 2)
    raw = await _token(org, user)

    # org_id="" -> the agent token carries the workspace (same path as the CLI).
    out = await list_events(token=raw, org_id="")
    evs = out["events"]
    assert [e["kind"] for e in evs] == ["commit", "snapshot"]  # ts DESC
    assert [e["id"] for e in evs] == [str(e2.id), str(e1.id)]
    assert evs[0]["payload"] == {"n": 2}
    assert evs[0]["actor_kind"] == "human"


async def test_list_events_since_cursor_excludes_older() -> None:
    org, user = await _workspace()
    await _emit(org, user, "snapshot", 1)  # older, must be excluded
    e2 = await _emit(org, user, "commit", 2)
    raw = await _token(org, user)

    # The cursor is inclusive (``ts >= since``, at-least-once): passing the
    # newest ts returns that event and nothing older.
    out = await list_events(token=raw, org_id="", since=e2.ts.isoformat())
    ids = [e["id"] for e in out["events"]]
    assert ids == [str(e2.id)]


async def test_list_events_is_rls_scoped_to_the_workspace() -> None:
    org_a, user_a = await _workspace()
    org_b, user_b = await _workspace()
    await _emit(org_a, user_a, "snapshot", 1)
    raw_b = await _token(org_b, user_b)

    out = await list_events(token=raw_b, org_id="")
    assert out["events"] == []  # B never sees A's events


def test_events_read_is_a_catalog_default_scope() -> None:
    assert "events:read" in VALID_SCOPE_KEYS
    # A read key, so a default-minted assistant observes coordination.
    assert "events:read" in DEFAULT_SCOPES


def test_list_events_requires_events_read() -> None:
    tok = _PRINCIPAL_SCOPE.set(["notes:read"])
    try:
        assert not _scope_permits("list_events")
    finally:
        _PRINCIPAL_SCOPE.reset(tok)
    tok = _PRINCIPAL_SCOPE.set(["events:read"])
    try:
        assert _scope_permits("list_events")
    finally:
        _PRINCIPAL_SCOPE.reset(tok)
