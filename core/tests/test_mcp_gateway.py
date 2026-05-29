"""Dynamic-toolset gateway: the HTTP surface is the three meta-tools
(search/describe/execute) over the full registry, not the ~140 concrete
tools. Verifies the small surface, semantic + lexical discovery, schema
loading with auth stripped, and dispatch with the principal injected.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder

import flow_mcp.gateway as gw
from flow_core.db import admin_session
from flow_core.embedder import set_embedder_override
from flow_core.services.auth import signup
from flow_mcp.gateway import describe_tools, execute_tool, gateway, search_tools
from flow_mcp.server import _PRINCIPAL


@pytest.fixture(autouse=True)
def _reset_index() -> Iterator[None]:
    # The embedding index + catalog are module globals cached across
    # calls; reset so each test starts clean (and an index built with
    # one embedder is not reused under another).
    gw._index = None
    gw._catalog_cache = None
    yield
    gw._index = None
    gw._catalog_cache = None


async def _signup_principal() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="GW",
        )
    return r.user_id, r.org_id


async def test_public_surface_is_only_meta_tools() -> None:
    names = {t.name for t in await gateway.list_tools()}
    assert names == {"ping", "search_tools", "describe_tools", "execute_tool"}


async def test_search_semantic_ranks_relevant_tool() -> None:
    set_embedder_override(FakeEmbedder)
    try:
        hits = await search_tools(query="list all my tasks", limit=8)
    finally:
        set_embedder_override(None)
    names = [h["name"] for h in hits]
    assert "list_tasks" in names
    # results carry a one-line summary + domain for the LLM to choose
    assert all({"name", "summary", "domain", "score"} <= set(h) for h in hits)


async def test_search_lexical_fallback_without_embedder() -> None:
    # No override and sentence-transformers absent in CI -> the lexical
    # branch runs; it must still surface the obvious match.
    from flow_core.embedder import embedder_available

    if embedder_available():
        pytest.skip("a real/overridden embedder is available; lexical path not exercised")
    hits = await search_tools(query="timer start stop", limit=8)
    assert any(h["name"] in {"start_timer", "stop_timer"} for h in hits)


async def test_search_domain_filter() -> None:
    set_embedder_override(FakeEmbedder)
    try:
        hits = await search_tools(query="anything", limit=50, domain="calendar")
    finally:
        set_embedder_override(None)
    assert hits and all(h["domain"] == "calendar" for h in hits)


async def test_describe_strips_auth_and_keeps_real_params() -> None:
    out = await describe_tools(names=["create_task"])
    assert len(out) == 1
    schema = out[0]["inputSchema"]
    props = schema["properties"]
    assert "token" not in props and "org_id" not in props
    assert "title" in props  # the real, LLM-facing param survives
    assert "token" not in schema.get("required", [])


async def test_describe_unknown_tool_is_soft_error() -> None:
    out = await describe_tools(names=["does_not_exist"])
    assert out[0]["name"] == "does_not_exist"
    assert "error" in out[0]


async def test_execute_dispatches_with_injected_principal() -> None:
    user_id, org_id = await _signup_principal()
    tok = _PRINCIPAL.set((user_id, org_id, None))
    try:
        # No token/org_id passed: execute_tool injects them from the
        # principal, exactly as the HTTP bearer path does.
        tag = await execute_tool(name="create_tag", arguments={"kind": "generic", "name": "gw-tag"})
        assert tag["name"] == "gw-tag"
        listed = await execute_tool(name="list_tags", arguments={})
        assert any(t["name"] == "gw-tag" for t in listed)
    finally:
        _PRINCIPAL.reset(tok)


async def test_execute_unknown_tool_is_soft_error() -> None:
    res = await execute_tool(name="nope", arguments={})
    assert "error" in res


async def test_execute_bad_arguments_point_to_describe_tools() -> None:
    # A wrong/missing/extra argument must come back as a structured
    # invalid_arguments error pointing at describe_tools (the schema),
    # not as a raw Python TypeError leaked from fn(**args). Validated
    # before the tool runs, so it needs no principal/DB.
    res = await execute_tool(name="create_tag", arguments={"bogus": 1})
    assert isinstance(res, dict) and isinstance(res.get("error"), dict)
    assert res["error"]["code"] == "invalid_arguments"
    assert "describe_tools" in res["error"]["hint"]


async def test_execute_domain_error_is_structured_with_code_and_params() -> None:
    # A domain/validation failure surfaces {code, detail, params} so the
    # caller can branch on the stable code and read the valid values,
    # instead of pattern-matching an opaque "Domain error" string
    # (the report's P4 keystone).
    user_id, org_id = await _signup_principal()
    tok = _PRINCIPAL.set((user_id, org_id, None))
    try:
        a = await execute_tool(name="create_note", arguments={"kind": "text", "text": "a"})
        b = await execute_tool(name="create_note", arguments={"kind": "text", "text": "b"})
        res = await execute_tool(
            name="link_notes",
            arguments={"parent_note_id": a["id"], "child_note_id": b["id"], "kind": "sibling"},
        )
        assert res["error"]["code"] == "note.link.kind_invalid"
        assert "hypha_of" in res["error"]["params"]["valid"]
    finally:
        _PRINCIPAL.reset(tok)


async def test_http_app_builds_and_serves_the_gateway() -> None:
    # Guards the wiring: the HTTP transport must serve the 3-meta-tool
    # gateway, not regress to the full registry.
    from flow_mcp.server_http import make_mcp_app

    app = make_mcp_app()
    assert app is not None
    assert gateway.settings.streamable_http_path == "/"
    names = {t.name for t in await gateway.list_tools()}
    assert "execute_tool" in names and "create_task" not in names


async def test_prewarm_builds_the_index_off_request_path() -> None:
    # Prewarm is what the API lifespan calls so the first search_tools
    # does not pay the ~140-text encode inline. Guards that it actually
    # builds the index (regression on the "appears hung" bug fixed by
    # this PR) and is safely a no-op on second invocation.
    set_embedder_override(FakeEmbedder)
    try:
        assert gw._index is None
        await gw.prewarm()
        assert gw._index is not None
        snapshot = gw._index
        await gw.prewarm()  # idempotent
        assert gw._index is snapshot
    finally:
        set_embedder_override(None)


def test_get_embedder_returns_singleton_in_prod_path() -> None:
    # Pre-fix shape returned a fresh LocalEmbedder per call, so every
    # search_tools paid the in-memory model load again and inflated the
    # working set toward the pod memory limit. Guards the cache.
    import flow_core.embedder as emb_mod
    from flow_core.embedder import LocalEmbedder, get_embedder, set_embedder_override

    set_embedder_override(None)
    emb_mod._singleton = None  # ensure cold start for this assertion
    try:
        first = get_embedder()
        second = get_embedder()
        assert isinstance(first, LocalEmbedder)
        assert first is second
    finally:
        emb_mod._singleton = None
