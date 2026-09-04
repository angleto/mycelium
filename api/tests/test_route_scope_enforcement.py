"""REST-side scope enforcement for scoped assistants (task c19f2f63, enabler B).

The MCP gateway gates every tool against the assistant's scope. These tokens also
authenticate here, so without the same gate a scoped assistant just stops speaking
MCP and regains full access -- the boundary would be a preference, not a permission.

Covered: the drift guard (the map matches the live route table), the
privilege-escalation fence (a scoped assistant must not be able to widen its own
scope), cross-surface consistency with TOOL_SCOPES, and the live 403 through a real
agent token over the real app.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api import route_scopes
from mycelium_api.app import create_app
from mycelium_api.main import app
from mycelium_api.route_scopes import HUMAN_ONLY, META, PUBLIC, ROUTE_SCOPES, scope_permits
from mycelium_core.mcp_scopes import VALID_SCOPE_KEYS
from mycelium_mcp.tool_scopes import TOOL_SCOPES


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _live_routes() -> set[tuple[str, str]]:
    a = create_app()
    out: set[tuple[str, str]] = set()
    for r in a.routes:
        methods = getattr(r, "methods", None)
        path = getattr(r, "path", None)
        if not methods or not path:
            continue
        for m in methods - {"HEAD", "OPTIONS"}:
            out.add((m, path))
    return out


# --------------------------------------------------------------------------
# Drift guard + map invariants
# --------------------------------------------------------------------------


def test_route_scopes_covers_every_live_route() -> None:
    """Fail-closed means an unmapped route is DENIED, so a route added without a
    scope entry would silently become unreachable for every scoped assistant.
    Catch it here instead."""
    live = _live_routes()
    mapped = set(ROUTE_SCOPES)
    missing = live - mapped
    stale = mapped - live
    assert not missing, f"routes missing a ROUTE_SCOPES entry: {sorted(missing)}"
    assert not stale, f"stale ROUTE_SCOPES entries (route no longer exists): {sorted(stale)}"


def test_route_scopes_reference_only_catalog_keys() -> None:
    # The sentinels are enumerated rather than "anything that is not a str":
    # a value that is neither a sentinel nor a key must still fall through to
    # the check below and fail, or a typo'd sentinel silently opens a route.
    sentinels = (PUBLIC, HUMAN_ONLY, META)
    bad: dict[tuple[str, str], list[str]] = {}
    for k, v in ROUTE_SCOPES.items():
        if any(v is sentinel for sentinel in sentinels):
            continue
        # A value is a single key or an any-of frozenset (kind multiplexer);
        # every member must be a real catalog key.
        keys = v if isinstance(v, frozenset) else {v}
        outside = {str(x) for x in keys if x not in VALID_SCOPE_KEYS}
        if outside:
            bad[k] = sorted(outside)
    assert not bad, f"ROUTE_SCOPES references keys absent from SCOPE_CATALOG: {bad}"


def test_link_multiplexer_routes_use_any_of() -> None:
    """Review #5: the note<->task link routes cannot map to one key (subject is
    a notes:write op, artifact a tasks:write one), so the gate uses an any-of
    frozenset and the handler enforces the exact key per kind."""
    for path in ("/notes/{note_id}/task-links", "/tasks/{task_id}/note-links"):
        req = ROUTE_SCOPES[("POST", path)]
        assert isinstance(req, frozenset)
        assert req == {"notes:write", "tasks:write"}
        # any-of: holding either key passes the coarse gate, holding neither fails
        assert scope_permits("POST", path, ["notes:write"])
        assert scope_permits("POST", path, ["tasks:write"])
        assert not scope_permits("POST", path, ["notes:read"])


def test_credential_and_assistant_routes_are_human_only() -> None:
    """The privilege-escalation fence. An assistant that could PATCH its own row
    would simply widen its own scope and undo the whole boundary; one that could
    mint an agent token would hand itself a fresh, unscoped credential."""
    for method, path in (
        ("POST", "/ai-assistants"),
        ("PATCH", "/ai-assistants/{assistant_id}"),
        ("DELETE", "/ai-assistants/{assistant_id}"),
        ("POST", "/ai-assistants/{assistant_id}/rotate"),
        ("POST", "/agent-tokens"),
    ):
        assert ROUTE_SCOPES.get((method, path)) is HUMAN_ONLY, f"{method} {path} must be HUMAN_ONLY"


def test_taxonomy_review_route_remaps() -> None:
    """Taxonomy review (task c19f2f63): the read-only notification routes moved
    off notifications:write onto the new notifications:read, and the search
    click write moved off search:read onto the new search:write."""
    for method, path in (
        ("GET", "/notifications"),
        ("GET", "/notifications/prefs"),
        ("GET", "/notifications/push/vapid-public-key"),
        ("GET", "/tasks/{task_id}/reminders"),
    ):
        assert ROUTE_SCOPES[(method, path)] == "notifications:read", (method, path)
    assert ROUTE_SCOPES[("POST", "/search/click")] == "search:write"


def test_rest_and_mcp_agree_on_equivalent_operations() -> None:
    """Cross-surface consistency: the same operation must cost the same key on
    both surfaces, or the cheaper one becomes the bypass."""
    for (method, path), tool in (
        (("GET", "/tasks"), "list_tasks"),
        (("POST", "/tasks"), "create_task"),
        (("GET", "/notes"), "list_notes"),
        (("POST", "/notes"), "create_note"),
        # The destructive pairs, where a divergence costs the most: the
        # part PURGE is the danger key on both surfaces, and the
        # restorable trash/restore pair is an ordinary note write on both.
        (("DELETE", "/notes/{note_id}/parts/{part_id}"), "delete_note_part"),
        (("POST", "/notes/{note_id}/parts/{part_id}/trash"), "trash_note_part"),
        (("POST", "/notes/{note_id}/parts/{part_id}/restore"), "restore_note_part"),
        (("GET", "/notes/{note_id}/parts:trashed"), "list_trashed_note_parts"),
    ):
        assert ROUTE_SCOPES.get((method, path)) == TOOL_SCOPES[tool], (
            f"{method} {path} and MCP {tool} disagree"
        )


def test_comment_writes_reach_the_same_rows_on_both_surfaces() -> None:
    """The annotation body/lifecycle writes are an any-of pair, not a single
    key, because the MCP twins are split across the two families that both
    address the same ``comments`` rows: ``update_comment`` / ``delete_comment``
    / ``restore_comment`` / ``replace_in_comment`` cost comments:write, while
    ``edit_annotation`` / ``delete_annotation`` cost annotations:write. Mapping
    the REST routes to one key would make whichever surface is cheaper the
    bypass; the any-of makes them agree by construction."""
    for method, path in (
        ("PATCH", "/annotations/{annotation_id}"),
        ("DELETE", "/annotations/{annotation_id}"),
        ("POST", "/annotations/{annotation_id}/body/patch"),
        ("POST", "/annotations/{annotation_id}/body/replace"),
        ("PATCH", "/annotations/{annotation_id}/body/stream"),
        ("POST", "/annotations/{annotation_id}/restore"),
    ):
        req = ROUTE_SCOPES[(method, path)]
        assert isinstance(req, frozenset), (method, path)
        assert req == {"annotations:write", "comments:write"}, (method, path)
        assert scope_permits(method, path, ["comments:write"])
        assert scope_permits(method, path, ["annotations:write"])
        assert not scope_permits(method, path, ["annotations:read"])
    # Both MCP families really do target the same rows, which is why the
    # any-of is the honest mapping rather than a widening.
    assert TOOL_SCOPES["update_comment"] == "comments:write"
    assert TOOL_SCOPES["edit_annotation"] == "annotations:write"


# --------------------------------------------------------------------------
# scope_permits semantics
# --------------------------------------------------------------------------


def test_no_scope_is_unrestricted() -> None:
    """Human sessions and bare agent tokens carry no scope list and must behave
    exactly as before this feature existed."""
    assert scope_permits("POST", "/tasks", None)
    assert scope_permits("PATCH", "/ai-assistants/{assistant_id}", None)
    assert scope_permits("GET", "/a/route/that/does/not/exist", None)


def test_scoped_assistant_is_confined() -> None:
    assert scope_permits("GET", "/tasks", ["tasks:read"])
    assert not scope_permits("POST", "/tasks", ["tasks:read"])


def test_human_only_and_unmapped_are_denied_even_with_every_scope() -> None:
    every = sorted(VALID_SCOPE_KEYS)
    assert not scope_permits("PATCH", "/ai-assistants/{assistant_id}", every)
    assert not scope_permits("GET", "/not/a/real/route", every)


def test_public_routes_stay_open() -> None:
    assert scope_permits("GET", "/healthz", [])
    assert route_scopes.required_scope("GET", "/healthz") is PUBLIC


# --------------------------------------------------------------------------
# Live enforcement through the real app
# --------------------------------------------------------------------------


async def _signup_owner(c: AsyncClient) -> dict[str, str]:
    su = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123", "workspace_name": "SCOPE"},
        )
    ).json()
    return {
        "Authorization": f"Bearer {su['token']}",
        "X-Workspace-Id": su["workspace_id"],
        "X-Workspace-Role": "owner",
    }


async def _assistant_headers(
    c: AsyncClient, owner: dict[str, str], scope: list[str]
) -> dict[str, str]:
    created = (
        await c.post(
            "/ai-assistants",
            headers=owner,
            json={"label": "A", "handle": f"a{uuid.uuid4().hex[:8]}", "scope": scope},
        )
    ).json()
    return {
        "Authorization": f"Bearer {created['raw_secret']}",
        "X-Workspace-Id": owner["X-Workspace-Id"],
    }


async def test_scoped_assistant_token_is_confined_over_http() -> None:
    """End to end: a read-only assistant can read tasks and nothing else -- and
    critically cannot reach the route that would widen its own scope."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner = await _signup_owner(c)
        workspace_id = owner["X-Workspace-Id"]

        created = (
            await c.post(
                "/ai-assistants",
                headers=owner,
                json={
                    "label": "Reader",
                    "handle": f"r{uuid.uuid4().hex[:8]}",
                    "scope": ["tasks:read"],
                },
            )
        ).json()
        assert "raw_secret" in created, created
        agent_headers = {
            "Authorization": f"Bearer {created['raw_secret']}",
            "X-Workspace-Id": workspace_id,
        }

        # In scope: unchanged behaviour.
        r = await c.get("/tasks", headers=agent_headers)
        assert r.status_code == 200, r.text

        # Out of scope: denied, with the stable machine code.
        r = await c.post("/tasks", headers=agent_headers, json={"title": "nope"})
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "agent.scope_denied"

        # The escalation route: denied even though it is "just" a write on a
        # row this assistant owns. Pre-fix this let a scoped assistant grant
        # itself every scope and walk out of the boundary.
        r = await c.patch(
            f"/ai-assistants/{created['assistant']['id']}",
            headers=agent_headers,
            json={"scope": sorted(VALID_SCOPE_KEYS)},
        )
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "agent.scope_denied"


async def test_bare_agent_token_keeps_full_access() -> None:
    """Regression: only a BOUND assistant carrying a scope list is confined.
    The CLI's bare PAT must be unaffected."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner = await _signup_owner(c)
        mint = (
            await c.post("/agent-tokens", headers=owner, json={"name": "cli", "scope": "cli"})
        ).json()
        pat_headers = {
            "Authorization": f"Bearer {mint['raw']}",
            "X-Workspace-Id": owner["X-Workspace-Id"],
        }
        r = await c.post("/tasks", headers=pat_headers, json={"title": "allowed"})
        assert r.status_code in (200, 201), r.text


async def test_gate_covers_route_that_bypasses_tenant_ctx() -> None:
    """POST /notes/quick-create resolves the bearer itself and opens an
    admin_session (set_config), bypassing the tenant_ctx dependency chain and
    X-Workspace-Id. A gate hooked onto tenant_ctx would silently miss it. The
    APP-level dependency runs before any of that, so a scoped assistant is
    denied here exactly like on the ordinary routes -- this is the reason the
    gate is app-level rather than folded into current_claims/tenant_ctx."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner = await _signup_owner(c)
        created = (
            await c.post(
                "/ai-assistants",
                headers=owner,
                json={
                    "label": "Reader",
                    "handle": f"r{uuid.uuid4().hex[:8]}",
                    "scope": ["tasks:read"],
                },
            )
        ).json()
        agent_headers = {
            "Authorization": f"Bearer {created['raw_secret']}",
            "X-Workspace-Id": owner["X-Workspace-Id"],
        }
        # notes:write route, and this assistant only holds tasks:read. The gate
        # must 403 BEFORE the endpoint's admin_session runs.
        r = await c.post("/notes/quick-create", headers=agent_headers, json={"text": "x"})
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "agent.scope_denied"


async def test_accepting_a_suggestion_needs_the_target_family_write_scope() -> None:
    """Propose-then-accept was a working bypass: accepting splices the proposed
    text INTO the note/task body, so ``comments:write`` (enough to REACH the
    accept route) must not be enough to accept -- the caller also needs write on
    the target family. This fence lives in the service, so it holds identically
    on the MCP tool. Denial changes nothing, so the same suggestion version is
    still acceptable by the properly-scoped assistant afterwards."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner = await _signup_owner(c)
        # Owner (full access) seeds a note part and a suggestion on it.
        note = (
            await c.post(
                "/notes", headers=owner, json={"kind": "text", "text": "The quick brown fox jumps."}
            )
        ).json()
        pid = (await c.get(f"/notes/{note['id']}", headers=owner)).json()["parts"][0]["id"]
        sug = (
            await c.post(
                "/annotations/suggestion",
                headers=owner,
                json={
                    "doc_kind": "note_part",
                    "doc_id": pid,
                    "original_text": "quick brown fox",
                    "proposed_text": "lazy dog",
                    "rationale": "shorter",
                },
            )
        ).json()

        # comments:write reaches the accept route (the gate lets it through) but
        # the service fence denies: accepting would rewrite the note body.
        commenter = await _assistant_headers(c, owner, ["comments:write"])
        r = await c.post(
            f"/annotations/{sug['id']}/accept",
            headers=commenter,
            json={"expected_version": sug["version"]},
        )
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "agent.scope_denied"
        # Nothing changed: the body is untouched and the suggestion still open.
        body = (await c.get(f"/notes/{note['id']}", headers=owner)).json()["parts"][0]["body"]
        assert body == "The quick brown fox jumps."

        # comments:write + notes:write: the accept goes through and splices.
        editor = await _assistant_headers(c, owner, ["comments:write", "notes:write"])
        r = await c.post(
            f"/annotations/{sug['id']}/accept",
            headers=editor,
            json={"expected_version": sug["version"]},
        )
        assert r.status_code == 200, r.text
        body = (await c.get(f"/notes/{note['id']}", headers=owner)).json()["parts"][0]["body"]
        assert body == "The lazy dog jumps."


async def test_link_route_enforces_scope_per_kind() -> None:
    """Review #5 end to end: a notes:write-only assistant reaches the link route
    (the any-of gate passes) and may create a subject link (a notes:write op),
    but the handler denies an artifact link (a tasks:write op) on the SAME
    route. Exactly mirrors the two separate MCP tools."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner = await _signup_owner(c)
        note = (
            await c.post("/notes", headers=owner, json={"kind": "text", "text": "linkable"})
        ).json()
        task = (await c.post("/tasks", headers=owner, json={"title": "linkable"})).json()
        writer = await _assistant_headers(c, owner, ["notes:write"])

        # subject == a notes:write operation -> allowed for a notes:write assistant.
        r = await c.post(
            f"/notes/{note['id']}/task-links",
            headers=writer,
            json={"task_id": task["id"], "kind": "subject"},
        )
        assert r.status_code in (200, 201), r.text

        # artifact == a tasks:write operation -> denied by the per-kind handler
        # fence, even though the any-of gate let the request reach the handler.
        r = await c.post(
            f"/notes/{note['id']}/task-links",
            headers=writer,
            json={"task_id": task["id"], "kind": "artifact"},
        )
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "agent.scope_denied"
