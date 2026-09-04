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
from mycelium_mcp.tool_scopes import DYNAMIC_TOOL_SCOPES, TOOL_SCOPES, required_keys


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
    """Every concrete tool maps to a scope (or to None = META) in exactly ONE
    of the static / dynamic maps. Missing entries would be denied to scoped
    assistants fail-closed, so catch it in CI."""
    registry = _registry_names()
    static = set(TOOL_SCOPES)
    dynamic = set(DYNAMIC_TOOL_SCOPES)
    both = static & dynamic
    assert not both, f"tools in BOTH TOOL_SCOPES and DYNAMIC_TOOL_SCOPES: {sorted(both)}"
    mapped = static | dynamic
    missing = registry - mapped
    stale = mapped - registry
    assert not missing, f"tools missing a scope entry: {sorted(missing)}"
    assert not stale, f"stale scope entries (tool no longer exists): {sorted(stale)}"


def test_tool_scopes_reference_only_catalog_keys() -> None:
    """No typo'd / invented scope key: an assistant can only ever be granted a
    key from SCOPE_CATALOG, so a tool gated on anything else is unreachable.
    Covers the static map and every scope an argument-dependent tool could
    require (its ``possible`` set enumerates the resolver's whole range).

    Read through ``required_keys`` rather than off the raw value, so that
    an any-of entry has EVERY member checked. Comparing the value itself
    to the catalogue would have passed a set containing a typo, because a
    frozenset is never a key: the check would have gone quiet exactly
    where it was needed most."""
    bad = {
        n: sorted(required_keys(n) - VALID_SCOPE_KEYS)  # type: ignore[operator]
        for n, s in TOOL_SCOPES.items()
        if s is not None and required_keys(n) - VALID_SCOPE_KEYS  # type: ignore[operator]
    }
    assert not bad, f"TOOL_SCOPES references keys absent from SCOPE_CATALOG: {bad}"
    bad_dyn = {
        n: sorted(possible - VALID_SCOPE_KEYS)
        for n, (_resolver, possible) in DYNAMIC_TOOL_SCOPES.items()
        if possible - VALID_SCOPE_KEYS
    }
    assert not bad_dyn, f"DYNAMIC_TOOL_SCOPES references keys absent from SCOPE_CATALOG: {bad_dyn}"


def test_catalog_keys_are_unique_and_categorised() -> None:
    keys = [s.key for s in SCOPE_CATALOG]
    assert len(keys) == len(set(keys)), "duplicate scope key in SCOPE_CATALOG"
    assert all(s.category in {"read", "write", "danger"} for s in SCOPE_CATALOG)
    danger = {s.key for s in SCOPE_CATALOG if s.category == "danger"}
    assert danger.isdisjoint(set(DEFAULT_SCOPES))
    for key in ("email:send", "memory:delete", "memory:admin", "ai:generate", "billing:write"):
        assert key in danger


def test_default_scopes_are_reads_only() -> None:
    """Taxonomy review round 2 (task c19f2f63): the mint default is least
    privilege -- READS ONLY. A fresh assistant observes but cannot mutate;
    writes and danger scopes are both opt-in, granted deliberately at mint."""
    reads = {s.key for s in SCOPE_CATALOG if s.category == "read"}
    assert set(DEFAULT_SCOPES) == reads
    assert not any(
        s.category in {"write", "danger"} and s.key in DEFAULT_SCOPES for s in SCOPE_CATALOG
    )


def test_taxonomy_review_reclassifications() -> None:
    """Sign-off of the taxonomy review (task c19f2f63): billing:read is a
    default read (it was the only read sitting in the danger tier), and
    notifications:read / search:write were added so a read no longer costs a
    write key. A regression here means a review decision was silently undone."""
    cat = {s.key: s.category for s in SCOPE_CATALOG}
    assert cat["billing:read"] == "read"
    assert "billing:read" in DEFAULT_SCOPES  # a read -> in the reads-only default
    assert cat["notifications:read"] == "read"
    assert cat["search:write"] == "write"
    assert "notifications:read" in VALID_SCOPE_KEYS
    assert "search:write" in VALID_SCOPE_KEYS
    # notifications:read (read) is a default grant; search:write (write) is
    # opt-in under the reads-only default policy.
    assert "notifications:read" in DEFAULT_SCOPES
    assert "search:write" not in DEFAULT_SCOPES


def test_delete_keys_are_danger_and_gate_hard_destruction() -> None:
    """Review #3: delete:notes / delete:tasks fence IRREVERSIBLE note/task
    destruction off the write key (parity with memory:delete). Opt-in danger,
    and the hard ops moved onto them."""
    cat = {s.key: s.category for s in SCOPE_CATALOG}
    assert cat["delete:notes"] == "danger"
    assert cat["delete:tasks"] == "danger"
    assert "delete:notes" not in DEFAULT_SCOPES
    assert "delete:tasks" not in DEFAULT_SCOPES
    assert TOOL_SCOPES["delete_note_part"] == "delete:notes"
    assert TOOL_SCOPES["remove_item"] == "delete:tasks"
    assert TOOL_SCOPES["clear_done"] == "delete:tasks"


def test_part_removal_is_reachable_without_the_danger_key() -> None:
    """The other half of review #3's own rule: only the IRREVERSIBLE op
    belongs on the danger key, so the RESTORABLE one must be an ordinary
    note write.

    Removing a block of a note is routine editing. While the purge was the
    only way to do it, that routine operation cost a danger key no ordinary
    assistant holds (``DEFAULT_SCOPES`` is reads-only), and the capability
    read as simply absent from the surface -- the tool was filtered out
    before an MCP client could ever see it. ``trash_note_part`` /
    ``restore_note_part`` restore the symmetry that ``delete_note`` (soft,
    notes:write) always had; ``delete_note_part`` keeps the danger key
    because it still destroys for good.
    """
    assert TOOL_SCOPES["trash_note_part"] == "notes:write"
    assert TOOL_SCOPES["restore_note_part"] == "notes:write"
    assert TOOL_SCOPES["list_trashed_note_parts"] == "notes:read"
    assert TOOL_SCOPES["delete_note_part"] == "delete:notes"
    tok = _PRINCIPAL_SCOPE.set(["notes:read", "notes:write"])
    try:
        assert _scope_permits("trash_note_part")
        assert _scope_permits("restore_note_part")
        assert _scope_permits("list_trashed_note_parts")
        assert not _scope_permits("delete_note_part")
    finally:
        _PRINCIPAL_SCOPE.reset(tok)


def test_comment_family_is_one_key() -> None:
    """``comments:write`` denotes the comment/suggestion collaboration
    family, so the whole quintet costs the same key.

    Before, an assistant holding comments:write could CREATE a comment
    (``add_comment``) and DESTROY it (``delete_comment``) but never rewrite
    it: every body-write path was annotations:write. A caller allowed to
    delete a comment but not to fix a typo in it is an incoherent surface,
    not a tighter one.
    """
    for tool in (
        "add_comment",
        "update_comment",
        "replace_in_comment",
        "delete_comment",
        "restore_comment",
    ):
        assert TOOL_SCOPES[tool] == "comments:write", tool
    tok = _PRINCIPAL_SCOPE.set(["comments:read", "comments:write"])
    try:
        assert _scope_permits("update_comment")
        assert _scope_permits("replace_in_comment")
        assert _scope_permits("restore_comment")
    finally:
        _PRINCIPAL_SCOPE.reset(tok)


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
    write/danger tool anywhere in the registry becomes callable -- including
    the argument-dependent write tools, which must be neither discoverable
    (listing) nor callable for any write kind."""
    reads = [s.key for s in SCOPE_CATALOG if s.category == "read"]
    tok = _PRINCIPAL_SCOPE.set(reads)
    try:
        leaked = [
            name
            for name, scope in TOOL_SCOPES.items()
            # ``required_keys`` normalises the any-of entries: a tool
            # satisfied by ANY key outside the read set is a leak if the
            # gate still lets it through.
            if scope is not None
            and not (required_keys(name) or frozenset()) <= set(reads)
            and _scope_permits(name)
        ]
        assert not leaked, f"read-only scope reached non-read tools: {sorted(leaked)}"
        # Dynamic write tools: hidden in a listing and denied for every kind.
        assert not _scope_permits("set_text_block_capability")
        assert not _scope_permits("patch_text_block_capability")
        for kind in ("annotation", "task_description"):
            assert not _scope_permits(
                "set_text_block_capability",
                {"kind": kind, "resource_id": "x", "expected_version": 1},
            )
    finally:
        _PRINCIPAL_SCOPE.reset(tok)


def test_dynamic_tool_scope_is_resolved_per_argument() -> None:
    """The bug enabler B's REST classification surfaced: an annotation body and
    a task description are the SAME MCP tool but need DIFFERENT scopes. A single
    static key over-granted one and denied the other; the resolver fixes it."""
    tok = _PRINCIPAL_SCOPE.set(["annotations:read"])
    try:
        # annotations:read reaches the comment body, NOT the task description.
        assert _scope_permits(
            "get_text_block_capability", {"kind": "annotation", "resource_id": "a"}
        )
        assert not _scope_permits(
            "get_text_block_capability", {"kind": "task_description", "resource_id": "t"}
        )
    finally:
        _PRINCIPAL_SCOPE.reset(tok)

    tok = _PRINCIPAL_SCOPE.set(["tasks:read"])
    try:
        # tasks:read reaches the task description, NOT the comment body (pre-fix
        # a tasks-only assistant was granted every annotation's body).
        assert _scope_permits(
            "get_text_block_capability", {"kind": "task_description", "resource_id": "t"}
        )
        assert not _scope_permits(
            "get_text_block_capability", {"kind": "annotation", "resource_id": "a"}
        )
    finally:
        _PRINCIPAL_SCOPE.reset(tok)

    # list_attachments follows the parent, not a fixed tasks:read.
    tok = _PRINCIPAL_SCOPE.set(["notes:read"])
    try:
        assert _scope_permits("list_attachments", {"note_id": "n"})
        assert not _scope_permits("list_attachments", {"task_id": "t"})
    finally:
        _PRINCIPAL_SCOPE.reset(tok)


def test_dynamic_tool_visibility_uses_possible_scopes() -> None:
    """With no arguments (search_tools / describe_tools) an argument-dependent
    tool is discoverable if the assistant holds ANY scope it could require, so
    an annotations-only assistant still finds the reader but not the writer."""
    tok = _PRINCIPAL_SCOPE.set(["annotations:read"])
    try:
        assert _scope_permits("get_text_block_capability")
        assert not _scope_permits("set_text_block_capability")
    finally:
        _PRINCIPAL_SCOPE.reset(tok)


def test_dynamic_tool_indeterminate_kind_fails_closed() -> None:
    """A call whose arguments do not pin a kind is denied, not waved through:
    the tool would reject the bad kind anyway, and fail-closed keeps a
    malformed argument from being a gate bypass."""
    tok = _PRINCIPAL_SCOPE.set(["annotations:read", "tasks:read", "notes:read"])
    try:
        assert not _scope_permits(
            "get_text_block_capability", {"kind": "bogus", "resource_id": "x"}
        )
        assert not _scope_permits("get_text_block_capability", {})
        assert not _scope_permits("list_attachments", {})
    finally:
        _PRINCIPAL_SCOPE.reset(tok)


async def test_execute_tool_reports_resolved_scope_for_dynamic_tool() -> None:
    """Through the gateway: a tasks-only assistant asking for an annotation body
    is denied, and the envelope names the scope THAT call needed."""
    tok = _PRINCIPAL_SCOPE.set(["tasks:read"])
    try:
        res = await execute_tool(
            name="get_text_block_capability",
            arguments={"kind": "annotation", "resource_id": "a"},
        )
        assert isinstance(res, dict) and isinstance(res.get("error"), dict)
        assert res["error"]["code"] == "mcp.scope_denied"
        assert res["error"]["required_scope"] == "annotations:read"
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
