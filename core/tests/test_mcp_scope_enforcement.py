"""Per-tool MCP scope enforcement (task c19f2f63, enabler B).

Two things are under test:

1. The DRIFT GUARD -- ``TOOL_SCOPES`` must stay in lockstep with the live tool
   registry. A newly added ``@mcp.tool()`` with no scope entry fails here
   rather than silently becoming unreachable for every scoped assistant
   (fail-closed) at runtime.
2. The GATE itself -- ``execute_tool`` is the only path to a concrete tool over
   the HTTP transport, so it is the security chokepoint; ``search_tools`` /
   ``describe_tools`` filter as defence-in-depth. Bare / stdio / human callers
   (no scope list) must keep their previous full access.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder

import mycelium_mcp.gateway as gw
from mycelium_core.embedder import set_embedder_override
from mycelium_core.mcp_scopes import DEFAULT_SCOPES, SCOPE_CATALOG, VALID_SCOPE_KEYS
from mycelium_mcp.gateway import describe_tools, execute_tool, search_tools
from mycelium_mcp.server import _PRINCIPAL_SCOPE, _scope_permits
from mycelium_mcp.server import mcp as _registry
from mycelium_mcp.tool_scopes import TOOL_SCOPES


@pytest.fixture(autouse=True)
def _reset_index() -> Iterator[None]:
    gw._index = None
    gw._catalog_cache = None
    yield
    gw._index = None
    gw._catalog_cache = None


def _registry_names() -> set[str]:
    return {t.name for t in _registry._tool_manager.list_tools()}


# --------------------------------------------------------------------------
# 1. Drift guard
# --------------------------------------------------------------------------


def test_tool_scopes_covers_every_registered_tool() -> None:
    """Every concrete tool maps to a scope (or to None = META). Missing entries
    would be denied to scoped assistants fail-closed, so catch it in CI."""
    registry = _registry_names()
    mapped = set(TOOL_SCOPES)
    missing = registry - mapped
    stale = mapped - registry
    assert not missing, f"tools missing a TOOL_SCOPES entry: {sorted(missing)}"
    assert not stale, f"stale TOOL_SCOPES entries (tool no longer exists): {sorted(stale)}"


def test_tool_scopes_reference_only_catalog_keys() -> None:
    """No typo'd / invented scope key: an assistant can only ever be granted a
    key from SCOPE_CATALOG, so a tool gated on anything else is unreachable."""
    bad = {n: s for n, s in TOOL_SCOPES.items() if s is not None and s not in VALID_SCOPE_KEYS}
    assert not bad, f"TOOL_SCOPES references keys absent from SCOPE_CATALOG: {bad}"


def test_catalog_keys_are_unique_and_categorised() -> None:
    keys = [s.key for s in SCOPE_CATALOG]
    assert len(keys) == len(set(keys)), "duplicate scope key in SCOPE_CATALOG"
    assert all(s.category in {"read", "write", "danger"} for s in SCOPE_CATALOG)
    # DEFAULT_SCOPES is the "everything except danger" mint policy: danger keys
    # must stay opt-in, so a freshly minted assistant can never send mail,
    # erase memory, move money or spend credits without an explicit grant.
    danger = {s.key for s in SCOPE_CATALOG if s.category == "danger"}
    assert danger.isdisjoint(set(DEFAULT_SCOPES))
    for key in ("email:send", "memory:delete", "memory:admin", "ai:generate", "billing:write"):
        assert key in danger


def test_meta_tools_are_exactly_the_bootstrap_trio() -> None:
    """META = callable with any scope. Keep this set tiny and auditable: it is
    the standing hole in the gate, so it may only hold self-identity /
    liveness / docs tools that expose no tenant data."""
    meta = {n for n, s in TOOL_SCOPES.items() if s is None}
    assert meta == {"whoami", "help", "ping"}


# --------------------------------------------------------------------------
# 2. The gate: _scope_permits
# --------------------------------------------------------------------------


def test_no_scope_list_keeps_full_access() -> None:
    """Bare agent tokens, stdio and human bearers have no scope list and must
    behave exactly as before enforcement existed."""
    assert _PRINCIPAL_SCOPE.get() is None
    assert _scope_permits("create_task")
    assert _scope_permits("memory_erase")
    assert _scope_permits("a_tool_that_does_not_exist")


def test_scoped_assistant_is_confined_to_its_keys() -> None:
    tok = _PRINCIPAL_SCOPE.set(["tasks:read"])
    try:
        assert _scope_permits("list_tasks")  # tasks:read -> granted
        assert not _scope_permits("create_task")  # tasks:write -> denied
        assert not _scope_permits("memory_erase")  # memory:delete -> denied
    finally:
        _PRINCIPAL_SCOPE.reset(tok)


def test_meta_tools_survive_an_empty_scope() -> None:
    """An empty list is deny-all, but the bootstrap trio must still work or the
    agent cannot even discover that it has no permissions."""
    tok = _PRINCIPAL_SCOPE.set([])
    try:
        assert _scope_permits("whoami")
        assert _scope_permits("ping")
        assert _scope_permits("help")
        assert not _scope_permits("list_tasks")
    finally:
        _PRINCIPAL_SCOPE.reset(tok)


def test_unmapped_tool_is_denied_fail_closed() -> None:
    """A tool absent from the map is denied to a scoped assistant even if its
    scope looks broad -- the drift guard keeps this from biting real tools."""
    tok = _PRINCIPAL_SCOPE.set(list(DEFAULT_SCOPES))
    try:
        assert not _scope_permits("brand_new_unmapped_tool")
    finally:
        _PRINCIPAL_SCOPE.reset(tok)


def test_read_only_scope_cannot_reach_any_write_tool() -> None:
    """The headline least-privilege property: grant every :read key and no
    write/danger tool anywhere in the registry becomes callable."""
    reads = [s.key for s in SCOPE_CATALOG if s.category == "read"]
    tok = _PRINCIPAL_SCOPE.set(reads)
    try:
        leaked = [
            name
            for name, scope in TOOL_SCOPES.items()
            if scope is not None and scope not in reads and _scope_permits(name)
        ]
        assert not leaked, f"read-only scope reached non-read tools: {sorted(leaked)}"
    finally:
        _PRINCIPAL_SCOPE.reset(tok)


# --------------------------------------------------------------------------
# 3. The gate through the gateway meta-tools
# --------------------------------------------------------------------------


async def test_execute_tool_denies_out_of_scope_before_dispatch() -> None:
    """Denial returns the structured envelope and never runs the tool -- note
    there is no DB/principal here, so a leak would surface as an error."""
    tok = _PRINCIPAL_SCOPE.set(["tasks:read"])
    try:
        res = await execute_tool(name="create_tag", arguments={"kind": "generic", "name": "x"})
        assert isinstance(res, dict) and isinstance(res.get("error"), dict)
        assert res["error"]["code"] == "mcp.scope_denied"
        assert res["error"]["tool"] == "create_tag"
        assert res["error"]["required_scope"] == "tags:write"
    finally:
        _PRINCIPAL_SCOPE.reset(tok)


async def test_execute_tool_denies_before_argument_validation() -> None:
    """Bad args on a forbidden tool must still read as scope_denied: the gate
    runs first so a caller cannot probe a tool's schema through error shapes."""
    tok = _PRINCIPAL_SCOPE.set(["tasks:read"])
    try:
        res = await execute_tool(name="create_tag", arguments={"bogus": 1})
        assert res["error"]["code"] == "mcp.scope_denied"
    finally:
        _PRINCIPAL_SCOPE.reset(tok)


async def test_execute_tool_allows_meta_under_a_restrictive_scope() -> None:
    tok = _PRINCIPAL_SCOPE.set([])
    try:
        res = await execute_tool(name="ping", arguments={})
        assert isinstance(res, str) and res.startswith("mycelium-core")
    finally:
        _PRINCIPAL_SCOPE.reset(tok)


async def test_execute_tool_unknown_name_still_reads_as_unknown() -> None:
    tok = _PRINCIPAL_SCOPE.set(["tasks:read"])
    try:
        res = await execute_tool(name="nope_not_a_tool", arguments={})
        assert "unknown tool" in str(res["error"])
    finally:
        _PRINCIPAL_SCOPE.reset(tok)


async def test_describe_tools_hides_schema_of_forbidden_tool() -> None:
    tok = _PRINCIPAL_SCOPE.set(["tasks:read"])
    try:
        out = await describe_tools(names=["create_tag", "list_tasks"])
        by_name = {o["name"]: o for o in out}
        assert "scope denied" in by_name["create_tag"]["error"]
        assert "inputSchema" not in by_name["create_tag"]
        assert "inputSchema" in by_name["list_tasks"]  # in scope -> full schema
    finally:
        _PRINCIPAL_SCOPE.reset(tok)


async def test_search_tools_only_surfaces_callable_tools() -> None:
    set_embedder_override(FakeEmbedder)
    tok = _PRINCIPAL_SCOPE.set(["tasks:read"])
    try:
        out = await search_tools(query="create a tag and list my tasks", limit=50)
        assert out, "scoped search should still surface the tools it may call"
        assert all(_scope_permits(r["name"]) for r in out)
        assert not any(r["name"] == "create_tag" for r in out)
    finally:
        _PRINCIPAL_SCOPE.reset(tok)
        set_embedder_override(None)


async def test_search_tools_unrestricted_for_full_access_caller() -> None:
    set_embedder_override(FakeEmbedder)
    try:
        out = await search_tools(query="create a tag", limit=50)
        assert any(r["name"] == "create_tag" for r in out)
    finally:
        set_embedder_override(None)


# --------------------------------------------------------------------------
# 4. Regressions for the escalations the adversarial audit found
# --------------------------------------------------------------------------


def test_credit_spending_tools_sit_on_a_danger_key() -> None:
    """A metered model call (LLM / TTS / STT) must cost a DANGER scope, so a
    default-scoped assistant cannot drain the org's credit budget. Four of these
    were classified notes:write until the audit; ``ai:generate`` exists to gate
    exactly this."""
    danger = {s.key for s in SCOPE_CATALOG if s.category == "danger"}
    for tool in (
        "distill_note",
        "append_message",
        "synthesize_speech",
        "transcribe_note",
        "kg_extract",
        "synthesize_season",
        "extract_cluster_pattern",
    ):
        assert TOOL_SCOPES[tool] in danger, f"{tool} spends credits under a non-danger scope"


def test_irreversible_erasure_requires_the_delete_scope() -> None:
    """``gdpr_erase_note`` hard-erases a note AND cascades to its memory blobs,
    so notes:write must not be enough to destroy memory -- otherwise the
    memory:delete danger scope is decorative."""
    for tool in ("gdpr_erase_note", "memory_erase", "memory_delete_blob"):
        assert TOOL_SCOPES[tool] == "memory:delete"


async def test_whoami_withholds_scoped_payloads_from_a_narrow_assistant() -> None:
    """whoami is META so bootstrap ALWAYS works -- but the exemption must not
    leak data. Its two enrichments return the same payloads as list_tasks and
    memory_search, so an assistant holding neither scope gets identity only."""
    import secrets
    import uuid

    from mycelium_core.db import admin_session, tenant_session
    from mycelium_core.models.agent_token import AgentToken
    from mycelium_core.models.ai_assistant import AiAssistant
    from mycelium_core.services import identities, tasks
    from mycelium_core.services.auth import signup
    from mycelium_mcp.server import _PRINCIPAL, whoami

    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="SCOPEWHO",
        )
    org, user = r.org_id, r.user_id
    handle = f"claude-{uuid.uuid4().hex[:8]}"
    async with tenant_session(str(org), str(user)) as s:
        assistant = AiAssistant(
            org_id=org,
            user_id=user,
            label="Narrow",
            handle=handle,
            scope=["calendar:read"],
            is_active=True,
        )
        s.add(assistant)
        await s.flush()
        await identities.ensure_for_ai_assistant(s, org_id=org, assistant_id=assistant.id)
        tok = AgentToken(
            org_id=org,
            user_id=user,
            name="t",
            prefix=f"mycelium_at_{secrets.token_hex(4)}",
            token_hash=secrets.token_bytes(32),
            scope="mcp",
            assistant_id=assistant.id,
        )
        s.add(tok)
        await s.flush()
        token_id = tok.id
        await tasks.create_task(
            s, org_id=org, actor_id=user, title="secret task", assignee_handle=handle
        )

    reset = _PRINCIPAL.set((user, org, token_id))
    stok = _PRINCIPAL_SCOPE.set(["calendar:read"])
    try:
        me = await whoami("", "")
    finally:
        _PRINCIPAL_SCOPE.reset(stok)
        _PRINCIPAL.reset(reset)

    # Bootstrap still works: an agent can always learn who it is and what it may do.
    assert me["identity"]["handle"] == handle
    assert me["scope"] == ["calendar:read"]
    assert "protocol" in me["pointers"]
    # ...but the scoped payloads are withheld, and say so (an empty list here
    # must not read as "there is nothing assigned to me").
    assert me["open_tasks"] == []
    assert me["memory_lane"]["recall"] == []
    assert any("open_tasks" in w for w in me["withheld"])
    assert any("memory_lane" in w for w in me["withheld"])
