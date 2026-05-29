"""Dynamic-toolset gateway over the full flow_mcp tool set.

Speakeasy "dynamic toolsets v2" pattern: the HTTP surface advertises
only three meta-tools — ``search_tools`` / ``describe_tools`` /
``execute_tool`` — instead of the ~140 concrete tools. The MCP
``tools/list`` payload drops from ~21k tokens to ~1k, which is the
single largest fixed cost an MCP client pays per conversation (input
schemas dominate the catalog). Tools are discovered semantically and
their schemas loaded on demand, so the client never carries schemas it
will not use.

The concrete tools stay registered on ``flow_mcp.server.mcp`` (the
internal *registry*): it is never served over HTTP here, but it owns
the canonical name -> (description, inputSchema, callable) mapping and
is still imported directly by the test suite and by the stdio
entrypoint (``main.py``), which keeps the legacy token-based flow.

Auth: the bearer middleware (``server_http``) validates the
``flow_at_…`` token and publishes the principal into ``_PRINCIPAL``
before dispatch. ``execute_tool`` injects the (now redundant)
``token``/``org_id`` tool args as empty strings, so the LLM never sees
or provides them — this also closes the ``org_id="me"`` magic-literal
ergonomics gap. The meta-tools themselves touch no tenant data and need
no auth args (the HTTP bearer still gates every request).
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from mcp.server.fastmcp import FastMCP

from flow_core import __version__
from flow_core.embedder import embed_batch, embedder_available, get_embedder
from flow_mcp.server import mcp as _registry

gateway: FastMCP = FastMCP("flow")

# Tool args carried only for the legacy stdio flow; under HTTP/OAuth the
# principal comes from the bearer, so these are injected as empties at
# dispatch time and stripped from every schema the LLM sees.
_AUTH_PARAMS: tuple[str, ...] = ("token", "org_id")

# Coarse domain tags for the optional structural prefilter in
# ``search_tools``. Embeddings carry the real semantic match; tags are a
# cheap, deterministic narrowing (first matching rule wins, else "misc").
_DOMAIN_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Navigation/relations must come FIRST: link/relation/resolve tools
    # also contain "note"/"task" in their names, so they would otherwise
    # be swallowed by the notes/tasks rules below.
    (
        "navigation",
        ("link", "backlink", "relation", "resolve", "prefix", "suggest", "graph", "dependency"),
    ),
    ("time", ("timer", "time_entry", "report")),
    # "recompute" intentionally dropped from calendar: recompute_schedule
    # still matches via "schedule", and keeping it here used to steal
    # memory_recompute_tiers into the calendar domain.
    ("calendar", ("event", "calendar", "holiday", "schedule")),
    ("orchestration", ("executor", "agent_run", "handoff", "offer", "claim", "dispatch", "tick")),
    ("workflow", ("workflow", "state", "transition")),
    ("memory", ("memory",)),
    (
        "notes",
        (
            "note",
            "part",
            "transcribe",
            "speech",
            "distill",
            "command",
            "conversation",
            "message",
            "turn",
        ),
    ),
    ("billing", ("rate", "credit", "budget", "invoice", "usage", "meter")),
    ("email", ("email",)),
    ("taxonomy", ("tag", "client", "project")),
    ("tasks", ("task", "comment", "checklist", "item", "revision")),
)

# Lazily built once: name -> (meta, normalized embedding). The index is
# small (~140 short strings) so a single build at first search is cheap
# and is never re-billed (embedder.embed does not meter).
_catalog_cache: list[dict[str, Any]] | None = None
_index: dict[str, list[float]] | None = None
_index_lock = asyncio.Lock()


def _domain_for(name: str) -> str:
    low = name.lower()
    for domain, kws in _DOMAIN_RULES:
        if any(kw in low for kw in kws):
            return domain
    return "misc"


def _summary(description: str | None) -> str:
    if not description:
        return ""
    for line in description.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _searchable_text(name: str, domain: str, description: str | None) -> str:
    # The domain prefix gives the embedder a categorical signal the
    # terse per-tool docstrings lack (the article's "categorical
    # overview" trick), improving discoverability without editing 140
    # docstrings. The name is de-snaked (list_tasks -> "list tasks") so
    # its words are real tokens for both the model and the lexical
    # fallback.
    return f"[{domain}] {name.replace('_', ' ')}: {description or ''}"


def _catalog() -> list[dict[str, Any]]:
    global _catalog_cache
    if _catalog_cache is None:
        cat: list[dict[str, Any]] = []
        for t in _registry._tool_manager.list_tools():
            domain = _domain_for(t.name)
            cat.append(
                {
                    "name": t.name,
                    "summary": _summary(t.description),
                    "domain": domain,
                    "text": _searchable_text(t.name, domain, t.description),
                }
            )
        _catalog_cache = cat
    return _catalog_cache


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm > 0 else vec


def _cosine(a: list[float], b: list[float]) -> float:
    # Both operands are normalized, so the dot product is the cosine.
    return sum(x * y for x, y in zip(a, b, strict=False))


def _lexical(query_tokens: set[str], text: str) -> float:
    # Fallback when no embedder is available: Jaccard-ish overlap.
    toks = set(text.lower().replace(":", " ").replace("[", " ").replace("]", " ").split())
    if not query_tokens or not toks:
        return 0.0
    return len(query_tokens & toks) / len(query_tokens)


async def _ensure_index() -> None:
    global _index
    if _index is not None:
        return
    async with _index_lock:
        if _index is None:  # still unbuilt after acquiring the lock
            emb = get_embedder()
            cat = _catalog()
            # Single batched forward pass: SentenceTransformer handles
            # ~140 short strings in one encode() call. The per-call
            # Python+tokenizer overhead dominated the previous sequential
            # loop and the first ``search_tools`` paid all of it inline,
            # making the request appear hung to the MCP client.
            results = await embed_batch(emb, [m["text"] for m in cat])
            _index = {m["name"]: _normalize(r.vector) for m, r in zip(cat, results, strict=True)}


async def prewarm() -> None:
    """Warm the embedding index off the request path so the first
    ``search_tools`` does not pay the ~140-embed startup cost inline.
    Safe to call multiple times (no-op after the first build) and from
    a server startup hook; failures are surfaced to the caller so the
    lifespan can decide whether to log-and-continue or fail boot."""
    if not embedder_available():
        return
    await _ensure_index()


def _strip_auth(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Return a copy of an input schema with the auth args removed, so
    the LLM is never asked to provide token/org_id."""
    if not schema:
        return {}
    out = dict(schema)
    props = dict(out.get("properties", {}))
    for p in _AUTH_PARAMS:
        props.pop(p, None)
    out["properties"] = props
    if "required" in out:
        out["required"] = [r for r in out["required"] if r not in _AUTH_PARAMS]
    return out


def _prune_schema(node: Any) -> Any:
    """Strip Pydantic-generated JSON-Schema noise for LLM consumption.

    Three rules, applied recursively:

    1. ``title`` keys are dropped: Pydantic auto-titles every property
       with its capitalized name (``"title": "Title"`` on ``title``),
       which adds bytes and zero information for an LLM that already
       sees the property key.
    2. ``anyOf: [{type:X}, {type:null}]`` (the Pydantic ``Optional``
       pattern) collapses to ``type: [X, "null"]`` when the non-null
       branch is a simple scalar/array — same semantics, ~60% smaller.
    3. ``default: null`` is dropped when present: it is the implicit
       default for any nullable column.

    Used by ``describe_tools(minimal=True)`` (the default). Tools that
    need the verbatim Pydantic output ask with ``minimal=False``.
    """
    if isinstance(node, list):
        return [_prune_schema(v) for v in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for k, v in node.items():
        # ``title`` is Pydantic metadata only when its value is a
        # string (the auto-capitalized property name). When ``title``
        # is itself a property name, its value is the property's
        # subschema (a dict) and must be preserved.
        if k == "title" and not isinstance(v, dict):
            continue
        if k == "default" and v is None:
            continue
        if k == "anyOf" and isinstance(v, list) and len(v) == 2:
            null_branch = next((b for b in v if b == {"type": "null"}), None)
            other = next((b for b in v if b != {"type": "null"}), None)
            if null_branch is not None and isinstance(other, dict):
                pruned = _prune_schema(other)
                if isinstance(pruned, dict) and isinstance(pruned.get("type"), str):
                    pruned = dict(pruned)
                    pruned["type"] = [pruned["type"], "null"]
                    out.update(pruned)
                    continue
            out[k] = [_prune_schema(b) for b in v]
            continue
        out[k] = _prune_schema(v)
    return out


@gateway.tool()
def ping() -> str:
    """Liveness probe; returns the flow-core version."""
    return f"flow-core {__version__}"


@gateway.tool()
async def search_tools(
    query: str, limit: int = 8, domain: str | None = None
) -> list[dict[str, Any]]:
    """Find the concrete Flow tools relevant to a natural-language goal.

    Returns ranked ``{name, summary, domain, score}`` entries. This is
    the entry point of the dynamic-toolset flow: search here, then call
    ``describe_tools`` for the schemas of the ones you want, then
    ``execute_tool`` to run them. ``domain`` optionally narrows to one
    of: tasks, notes, navigation, time, calendar, memory, orchestration,
    workflow, taxonomy, billing, email, misc.
    """
    cat = {m["name"]: m for m in _catalog()}
    names = [n for n, m in cat.items() if domain is None or m["domain"] == domain]
    if embedder_available():
        await _ensure_index()
        index = _index or {}
        qv = _normalize((await get_embedder().embed(query)).vector)
        scored = [(_cosine(qv, index[n]), n) for n in names if n in index]
    else:
        qtok = set(query.lower().split())
        scored = [(_lexical(qtok, cat[n]["text"]), n) for n in names]
    scored.sort(key=lambda s: s[0], reverse=True)
    out: list[dict[str, Any]] = []
    for score, name in scored[: max(1, limit)]:
        m = cat[name]
        out.append(
            {"name": name, "summary": m["summary"], "domain": m["domain"], "score": round(score, 4)}
        )
    return out


@gateway.tool()
async def describe_tools(names: list[str], minimal: bool = True) -> list[dict[str, Any]]:
    """Return ``{name, description, inputSchema}`` for the named tools
    (as found via ``search_tools``). The auth args (token/org_id) are
    stripped: they are injected automatically at execution. Unknown
    names come back as ``{name, error}`` instead of failing the call.

    ``minimal`` (default True): emit a pruned JSON-Schema with
    Pydantic-redundant ``title`` keys removed, ``Optional`` ``anyOf``
    branches collapsed to ``type: [..., "null"]``, and implicit
    ``default: null`` dropped. ~60% smaller payload, identical
    semantics. Pass ``minimal=False`` for the verbatim Pydantic output.
    """
    out: list[dict[str, Any]] = []
    for name in names:
        tool = _registry._tool_manager.get_tool(name)
        if tool is None:
            out.append({"name": name, "error": "unknown tool; call search_tools first"})
            continue
        schema = _strip_auth(tool.parameters)
        if minimal:
            schema = _prune_schema(schema)
        out.append(
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": schema,
            }
        )
    return out


@gateway.tool()
async def execute_tool(name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Run a concrete Flow tool by name with ``arguments`` (the schema
    from ``describe_tools``, minus the auth args). token/org_id are
    injected from the authenticated principal, so omit them. Unknown
    names return a structured error; tool-level errors propagate
    unchanged so the caller sees the real domain/validation message."""
    tool = _registry._tool_manager.get_tool(name)
    if tool is None:
        return {"error": f"unknown tool: {name}; call search_tools first"}
    args = dict(arguments or {})
    props = (tool.parameters or {}).get("properties", {})
    for p in _AUTH_PARAMS:
        if p in props:
            args.setdefault(p, "")
    result = tool.fn(**args)
    if tool.is_async:
        result = await result
    return result


__all__ = ["describe_tools", "execute_tool", "gateway", "ping", "prewarm", "search_tools"]
