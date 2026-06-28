"""Dynamic-toolset gateway: the HTTP surface is the three meta-tools
(search/describe/execute) over the full registry, not the ~140 concrete
tools. Verifies the small surface, semantic + lexical discovery, schema
loading with auth stripped, and dispatch with the principal injected.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder

import mycelium_mcp.gateway as gw
from mycelium_core.db import admin_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.services.auth import signup
from mycelium_mcp.gateway import describe_tools, execute_tool, gateway, search_tools
from mycelium_mcp.server import _PRINCIPAL


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
        # FakeEmbedder is a length-normalized bag-of-words: the over-generic
        # token "list" matches all ~18 list_* tools, so a query leading with
        # it buries the richer-docstring list_tasks under its short siblings
        # (and the count_tasks sibling added in 080a9c13). A natural task
        # query without that token still surfaces it -- the discovery
        # property under test; a real embedder ranks list_tasks first either
        # way.
        hits = await search_tools(query="show my tasks", limit=8)
    finally:
        set_embedder_override(None)
    names = [h["name"] for h in hits]
    assert "list_tasks" in names
    # results carry a one-line summary + domain for the LLM to choose;
    # hits are pre-sorted most-relevant-first, so no numeric score is
    # emitted in the payload (the LLM acts on order + summary).
    assert all({"name", "summary", "domain"} <= set(h) for h in hits)
    assert all("score" not in h for h in hits)


async def test_search_lexical_fallback_without_embedder() -> None:
    # No override and sentence-transformers absent in CI -> the lexical
    # branch runs; it must still surface the obvious match.
    from mycelium_core.embedder import embedder_available

    if embedder_available():
        pytest.skip("a real/overridden embedder is available; lexical path not exercised")
    hits = await search_tools(query="timer start stop", limit=8)
    assert any(h["name"] in {"start_timer", "stop_timer"} for h in hits)


async def test_domain_dominates_top_k_for_a_clear_in_domain_query() -> None:
    # Common-case contract (task 26efb287): a clearly in-domain query still
    # yields an all-in-domain top-k -- the soft penalty is large enough that
    # off-domain tools do not crack the top of a focused search. The 'search'
    # bucket is allowed too (never penalized), as before.
    set_embedder_override(FakeEmbedder)
    try:
        hits = await search_tools(query="timer start stop pause resume", limit=8, domain="time")
    finally:
        set_embedder_override(None)
    assert hits and all(h["domain"] in {"time", "search"} for h in hits)


async def test_domain_is_a_soft_downrank_not_a_hard_exclude() -> None:
    # The durable refinement (task 26efb287): off-domain tools are demoted,
    # not removed. With a wide enough window a strong off-domain match is
    # still reachable below the in-domain hits, instead of being hidden as
    # the old hard prefilter did.
    set_embedder_override(FakeEmbedder)
    try:
        hits = await search_tools(query="timer", limit=60, domain="time")
    finally:
        set_embedder_override(None)
    domains = {h["domain"] for h in hits}
    assert "time" in domains
    assert domains - {"time", "search"}, "off-domain tools must remain reachable (soft, not hard)"


async def test_cross_cutting_search_survives_domain_prefilter() -> None:
    # Regression for the discovery defect that drove an agent to enumerate
    # tasks instead of filtering: ``search`` (domain 'search') is the only
    # tool exposing a task tag/free-text facet, and an agent scoping to
    # domain='tasks' must still reach it. Lexical path (CI has no embedder)
    # with a query that matches the search() docstring, so it ranks even
    # without semantics; the point under test is that it is ELIGIBLE at all.
    from mycelium_core.embedder import embedder_available

    if embedder_available():
        pytest.skip("real embedder present; lexical eligibility path not exercised")
    hits = await search_tools(
        query="unified search across tasks notes and memory blobs",
        limit=8,
        domain="tasks",
    )
    names = [h["name"] for h in hits]
    assert "search" in names  # cross-cutting tool not hard-excluded by domain='tasks'


async def test_list_tools_expose_service_filters_in_schema() -> None:
    # list_tasks must forward the service/REST filters it used to drop
    # (tag/parent/assignee/archived/deleted), and list_notes must expose the
    # whole-corpus free-text ``q``. Schema-level so it needs no DB.
    out = {d["name"]: d for d in await describe_tools(names=["list_tasks", "list_notes"])}
    lt = set(out["list_tasks"]["inputSchema"]["properties"])
    assert {
        "tag_id",
        "parent_task_id",
        "assignee_id",
        "include_archived",
        "include_deleted",
    } <= lt
    assert "q" in out["list_notes"]["inputSchema"]["properties"]


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
    from mycelium_mcp.server_http import make_mcp_app

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
    import mycelium_core.embedder as emb_mod
    from mycelium_core.embedder import LocalEmbedder, get_embedder, set_embedder_override

    set_embedder_override(None)
    emb_mod._singleton = None  # ensure cold start for this assertion
    try:
        first = get_embedder()
        second = get_embedder()
        assert isinstance(first, LocalEmbedder)
        assert first is second
    finally:
        emb_mod._singleton = None


async def test_telemetry_records_meta_tool_calls(tmp_path, monkeypatch) -> None:
    # With MYCELIUM_MCP_TELEMETRY set, each meta-tool call appends one JSONL
    # row carrying only {ts, kind, tool, result_bytes} -- enough for the
    # usage_report aggregation, but never arguments or payloads.
    telem = tmp_path / "telem.jsonl"
    monkeypatch.setenv("MYCELIUM_MCP_TELEMETRY", str(telem))
    user_id, org_id = await _signup_principal()
    tok = _PRINCIPAL.set((user_id, org_id, None))
    try:
        await execute_tool(name="create_tag", arguments={"kind": "generic", "name": "telem-tag"})
        set_embedder_override(FakeEmbedder)
        try:
            await search_tools(query="tags", limit=3)
        finally:
            set_embedder_override(None)
    finally:
        _PRINCIPAL.reset(tok)

    rows = [json.loads(line) for line in telem.read_text().splitlines() if line.strip()]
    pairs = {(r["kind"], r["tool"]) for r in rows}
    assert ("execute", "create_tag") in pairs
    assert ("search", "search_tools") in pairs
    assert all(r["result_bytes"] > 0 for r in rows)
    # The row schema is fixed and payload-free: no leak of args/results.
    assert all(set(r) == {"ts", "kind", "tool", "result_bytes"} for r in rows)


async def test_telemetry_is_noop_when_unset(tmp_path, monkeypatch) -> None:
    # Default (env unset): _record must not write and must not raise, so
    # production pays only a single env lookup per call.
    monkeypatch.delenv("MYCELIUM_MCP_TELEMETRY", raising=False)
    gw._record("execute", "create_tag", {"id": "x"})  # no-op, no exception
    assert not list(tmp_path.iterdir())


# ── MCP gateway I/O metering (op='mcp_io', WS task e30d188e) ──────────


def test_estimate_tokens_is_char_over_4() -> None:
    """The coarse token estimate is ceil(compact-JSON-chars / 4)."""
    payload = {"a": "x" * 40, "b": [1, 2, 3]}
    n = len(json.dumps(payload, separators=(",", ":"), default=str))
    assert gw._estimate_tokens(payload) == (n + 3) // 4
    assert gw._estimate_tokens([]) == 1  # "[]" -> ceil(2/4)


async def _seed_card(org_id: uuid.UUID, user_id: uuid.UUID, *, with_card: bool) -> None:
    from decimal import Decimal

    from mycelium_core.db import tenant_session
    from mycelium_core.services import billing

    async with tenant_session(str(org_id), str(user_id)) as s:
        await billing.grant_credits(s, org_id=org_id, actor_id=user_id, amount=Decimal(100))
        if with_card:
            await billing.upsert_rate_card(
                s,
                org_id=org_id,
                actor_id=user_id,
                model_id=gw._MCP_IO_MODEL,
                provider="platform",
                values={
                    "credits_per_input": Decimal("0.001"),
                    "credits_per_output": Decimal("0.001"),
                },
            )


async def _mcp_io_records(org_id: uuid.UUID, user_id: uuid.UUID) -> list:
    from sqlalchemy import select

    from mycelium_core.db import tenant_session
    from mycelium_core.models.billing import UsageRecord

    async with tenant_session(str(org_id), str(user_id)) as s:
        return list(
            (await s.execute(select(UsageRecord).where(UsageRecord.op == "mcp_io"))).scalars().all()
        )


async def test_mcp_io_meters_the_wallet_when_rate_card_configured() -> None:
    """An MCP meta-tool call debits the wallet under op='mcp_io' /
    model_id='mcp:gateway', attributed to actor_kind 'mcp_token' (NOT the
    autonomous 'system' kind), once a rate card exists."""
    from mycelium_core.db import tenant_session
    from mycelium_core.services import billing

    user_id, org_id = await _signup_principal()
    await _seed_card(org_id, user_id, with_card=True)
    async with tenant_session(str(org_id), str(user_id)) as s:
        before = await billing.balance(s, org_id=org_id)
    tok = _PRINCIPAL.set((user_id, org_id, None))
    try:
        await execute_tool(name="list_tags", arguments={})
    finally:
        _PRINCIPAL.reset(tok)
    recs = await _mcp_io_records(org_id, user_id)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.model_id == gw._MCP_IO_MODEL
    assert rec.actor_kind == "mcp_token"
    assert rec.units_in > 0 and rec.units_out > 0
    async with tenant_session(str(org_id), str(user_id)) as s:
        after = await billing.balance(s, org_id=org_id)
    assert after < before


async def test_mcp_io_is_free_without_rate_card() -> None:
    """Without a 'mcp:gateway' rate card the call is free: no usage row, no
    debit -- so OSS/dev/CI (and any org that opts out) are unchanged."""
    from mycelium_core.db import tenant_session
    from mycelium_core.services import billing

    user_id, org_id = await _signup_principal()
    await _seed_card(org_id, user_id, with_card=False)
    async with tenant_session(str(org_id), str(user_id)) as s:
        before = await billing.balance(s, org_id=org_id)
    tok = _PRINCIPAL.set((user_id, org_id, None))
    try:
        await execute_tool(name="list_tags", arguments={})
    finally:
        _PRINCIPAL.reset(tok)
    assert await _mcp_io_records(org_id, user_id) == []
    async with tenant_session(str(org_id), str(user_id)) as s:
        assert await billing.balance(s, org_id=org_id) == before
