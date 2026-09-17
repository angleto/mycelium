"""Dynamic-toolset gateway over the full mycelium_mcp tool set.

Speakeasy "dynamic toolsets v2" pattern: the HTTP surface advertises
only three meta-tools — ``search_tools`` / ``describe_tools`` /
``execute_tool`` — instead of the ~140 concrete tools. The MCP
``tools/list`` payload drops from ~21k tokens to ~1k, which is the
single largest fixed cost an MCP client pays per conversation (input
schemas dominate the catalog). Tools are discovered semantically and
their schemas loaded on demand, so the client never carries schemas it
will not use.

The concrete tools stay registered on ``mycelium_mcp.server.mcp`` (the
internal *registry*): it is never served over HTTP here, but it owns
the canonical name -> (description, inputSchema, callable) mapping and
is still imported directly by the test suite and by the stdio
entrypoint (``main.py``), which keeps the legacy token-based flow.

Auth: the bearer middleware (``server_http``) validates the
``mycelium_at_…`` token and publishes the principal into ``_PRINCIPAL``
before dispatch. ``execute_tool`` injects the (now redundant)
``token``/``org_id`` tool args as empty strings, so the LLM never sees
or provides them — this also closes the ``org_id="me"`` magic-literal
ergonomics gap. The meta-tools themselves touch no tenant data and need
no auth args (the HTTP bearer still gates every request).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import json
import logging
import math
import os
import re
import uuid
from decimal import Decimal
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

from mycelium_core import __version__
from mycelium_core.db import tenant_session
from mycelium_core.embedder import (
    EmbedSide,
    embed_batch,
    embedder_available,
    get_embedder,
)
from mycelium_core.errors import DomainError, jsonable_params
from mycelium_core.i18n import MessageCode
from mycelium_core.mcp_scopes import HUMAN_ONLY
from mycelium_core.models.billing import CostBasis
from mycelium_core.services import billing
from mycelium_mcp.server import _INSTRUCTIONS_BODY, _PRINCIPAL, _scope_permits
from mycelium_mcp.server import mcp as _registry
from mycelium_mcp.tool_scopes import TOOL_SCOPES, UNMAPPED, required_scope_for_call

_log = logging.getLogger("mycelium.mcp.gateway")

#: The calling convention of THIS surface, joined to the surface-neutral body.
#: It has to be said out loud: the client sees four tools and no hint that the
#: rest of the catalogue exists, while every description and every result it
#: will read writes a tool the compact way, ``name(arg=...)``. Read as a call
#: that notation is a lie here, and a client that believes it asks its host for
#: a tool that does not exist and is told so by the HOST, not by Mycelium -- a
#: refusal that reads like a missing capability and is not one. Measured on
#: 2026-09-17 from a real session: 11 successful calls, then "No such tool
#: available: mcp__claude_ai_Mycelium__help", because the shared instructions
#: said "read platform docs with help(topic)". Naming the notation is what
#: keeps the catalogue's descriptions surface-neutral instead of rewriting
#: every one of them.
_CALLING_GATEWAY = (
    "CALLING CONVENTION, read this before your first call: the only tools on this "
    "surface are search_tools, describe_tools, execute_tool and ping. Every other "
    "Mycelium tool -- 'whoami', 'help', 'memory_write', the whole catalogue -- is NOT a "
    "tool here: you run it as execute_tool(name='<tool>', arguments={...}). Tool descriptions "
    "and tool results name a tool together with its arguments in the compact source-like "
    "form a reader expects -- a write_hint spelling out memory_write and its parameters, "
    "for instance. That is the tool's name and signature, it is never a tool you can call "
    "directly. Find tools with search_tools, load their schemas with describe_tools, and "
    "read the platform documentation with "
    "execute_tool(name='help', arguments={'topic': '<topic>'})."
)

# The convention comes FIRST here, unlike on the registry surface. The body's
# second sentence is an imperative -- run 'whoami' -- and an instruction on how
# to act is worth nothing after the instruction to act.
gateway: FastMCP = FastMCP("mycelium", instructions=f"{_CALLING_GATEWAY} {_INSTRUCTIONS_BODY}")

# A platform usage fee denominated in payload tokens for the MCP gateway
# path (decision 2026-06-02; 90e4db3e §6/§13.2, task e30d188e). It is NOT a
# passthrough of the caller's model spend (Mycelium never observes that). Free
# unless an org configures a rate card for this model_id, so OSS/dev/CI are
# unchanged -- exactly like the bundled-embedder seam.
_MCP_IO_MODEL = "mcp:gateway"

# Opt-in usage telemetry. When ``MYCELIUM_MCP_TELEMETRY`` names a writable
# path, every meta-tool call appends one JSONL row recording only the
# tool name and the serialized result size — never arguments or payloads,
# so the trace carries no tenant data. Unset (the default, incl. prod and
# the test suite) it is a single env lookup per call: zero overhead. The
# companion ``scripts/perf/usage_report.py`` aggregates the file into a
# per-tool frequency x response-cost report, which is what turns the
# "~12% of tokens" attribution into a measured per-tool breakdown.
_TELEMETRY_ENV = "MYCELIUM_MCP_TELEMETRY"


async def _expand_prefixes(args: dict[str, Any]) -> dict[str, Any] | None:
    """Replace any short task/note id in ``args`` with the full uuid it names.

    Returns an error envelope when a prefix names nothing or names more than
    one row, and ``None`` when every argument is fine (``args`` is mutated in
    place). Ambiguity is REFUSED rather than resolved to the freshest
    candidate: ``resolve_prefix`` orders its matches, so picking the first
    would always succeed and would sometimes write to the wrong entity, which
    the caller has no way to notice. The refusal names the candidates with
    their full ids, so the next call is a correct one rather than a guess.

    Costs one query per prefix-shaped argument, and nothing at all for a
    caller that passes full uuids.
    """
    principal = _PRINCIPAL.get()
    if principal is None:
        return None  # stdio / unauthenticated: no session to resolve against
    user_id, org_id, _token_id = principal
    pending: list[tuple[str, str, tuple[str, ...]]] = []
    for key, value in args.items():
        if not isinstance(value, str) or len(value) >= _FULL_UUID_LEN:
            continue
        if not _PREFIX_RE.match(value):
            continue
        kinds = _PREFIX_ARGS.get(key)
        if kinds is None and key in _PREFIX_ARGS_BY_KIND:
            discriminator, by_value = _PREFIX_ARGS_BY_KIND[key]
            sibling = args.get(discriminator)
            if isinstance(sibling, str):
                kinds = by_value.get(sibling.strip().lower())
        if kinds is not None:
            pending.append((key, value, kinds))
    if not pending:
        return None
    from mycelium_core.services import lookup as lookup_svc

    async with tenant_session(str(org_id), str(user_id), actor_kind="mcp_token") as session:
        for key, value, kinds in pending:
            try:
                # Archived AND deleted are both in scope here, which is wider
                # than what a picker would ask for. Expanding a prefix is name
                # resolution, not authorization: ``restore_task`` and
                # ``restore_note`` exist precisely to act on a soft-deleted
                # row, and refusing to resolve its id would make them
                # unreachable by the only id form this surface hands out.
                # Whether the operation is legal stays the tool's decision.
                matches = await lookup_svc.resolve_prefix(
                    session,
                    prefix=value,
                    kinds=kinds,
                    include_archived=True,
                    include_deleted=True,
                )
            except DomainError:
                # Not a usable prefix at all (too short, not hex). Leave it
                # alone: the tool's own uuid parsing gives the better message.
                continue
            if not matches:
                return {
                    "error": {
                        "code": MessageCode.ID_PREFIX_UNKNOWN.value,
                        "detail": (
                            f"no {' or '.join(kinds)} has an id starting with {value!r}; "
                            "pass the full uuid, or find it with "
                            "execute_tool(name='resolve_prefix', arguments={'prefix': ...})"
                        ),
                        "argument": key,
                        "prefix": value,
                    }
                }
            if len(matches) > 1:
                return {
                    "error": {
                        "code": MessageCode.ID_PREFIX_AMBIGUOUS.value,
                        "detail": (
                            f"{len(matches)} entities have an id starting with {value!r}; "
                            "pass the full uuid of the one you mean"
                        ),
                        "argument": key,
                        "prefix": value,
                        "candidates": [
                            {"kind": m.kind, "id": str(m.id), "title": m.title} for m in matches
                        ],
                    }
                }
            args[key] = str(matches[0].id)
    return None


def _wire(result: Any) -> TextContent:
    """Serialize a concrete tool's result the way it will be read: compact
    JSON, one copy, raw UTF-8.

    FastMCP renders any non-``str`` return with
    ``pydantic_core.to_json(..., indent=2)`` (``func_metadata``'s
    ``_convert_to_content``), and ``execute_tool`` is the only meta-tool that
    reaches that path: ``search_tools`` and ``describe_tools`` annotate a
    ``list[dict]`` return, so FastMCP builds an output schema for them and the
    client reads the already-compact ``structuredContent`` instead. Measured
    on 2026-09-17 over 721 recorded gateway calls, that indentation alone was
    12.3% of every result token this server has put into a context window
    (67,873 of 553,547), and all of it on the execute path.

    Returning a ``TextContent`` rather than annotating a structured return is
    what keeps it at ONE copy on the wire: a structured tool sends the
    payload twice (``structuredContent`` plus the text block) and leaves it to
    the client which one it charges for. ``ensure_ascii=False`` because the
    content is largely Italian and ``\\uXXXX`` escapes cost several tokens per
    accented character.

    What it does not cover: anything inside the payload. This is the envelope;
    the content is slimmed by the serializers in ``server.py``.
    """
    return TextContent(
        type="text",
        text=json.dumps(result, separators=(",", ":"), default=str, ensure_ascii=False),
    )


def _result_bytes(result: Any) -> int:
    """UTF-8 byte size of a tool result as the MCP client will read it
    (compact JSON, ``default=str`` for uuid/Decimal/datetime)."""
    return len(json.dumps(result, separators=(",", ":"), default=str).encode("utf-8"))


def _record(kind: str, tool: str, result: Any) -> None:
    """Append one telemetry row ``{ts, kind, tool, result_bytes}`` when
    ``MYCELIUM_MCP_TELEMETRY`` is set; otherwise a no-op. Best-effort: any
    I/O or serialization failure is swallowed so telemetry can never
    break a real call. ``kind`` is one of ``search`` / ``describe`` /
    ``execute``; for ``execute`` the ``tool`` is the concrete tool name."""
    path = os.environ.get(_TELEMETRY_ENV)
    if not path:
        return
    try:
        row = {
            "ts": dt.datetime.now(dt.UTC).isoformat(),
            "kind": kind,
            "tool": tool,
            "result_bytes": _result_bytes(result),
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except (OSError, TypeError, ValueError):
        # Telemetry is observational; never surface its failures.
        return


#: The estimator lives in ``billing`` with the constant it divides by: this
#: module and ``scripts/perf/usage_report.py`` both bill or report on the same
#: telemetry, and two copies of a ratio are two chances to be measured once and
#: corrected once. Re-exported under the old name so the call sites below and
#: their tests keep reading as they did.
_estimate_tokens = billing.estimate_tokens


async def _meter_io(tool: str, request: Any, result: Any) -> None:
    """Charge the per-call MCP gateway usage fee (``op='mcp_io'``,
    ``model_id='mcp:gateway'``) on the I/O token estimate.

    Free unless the org configured a rate card for the model. Distinct
    surface from ``op='llm'``: if a tool runs an internal LLM, those tokens
    are metered there, so there is no double-count. Best-effort: the result
    is already computed, so any metering failure is logged and swallowed --
    billing must never break an MCP call.

    operation_id is a fresh id per call: every MCP call is a distinct
    billable usage event and is charged once. (A content hash, the other
    candidate, would collapse repeated identical calls -- exactly the usage
    the fee exists to bill -- so it is rejected here; the unique constraint
    makes a per-call id idempotent against true duplicate delivery.)
    """
    principal = _PRINCIPAL.get()
    if principal is None:
        return  # stdio / unauthenticated: no gateway billing context
    user_id, org_id, token_id = principal
    operation_id = f"mcp_io:{token_id}:{uuid.uuid4().hex}"
    try:
        async with tenant_session(str(org_id), str(user_id), actor_kind="mcp_token") as s:
            await billing.meter_if_billable(
                s,
                org_id=org_id,
                actor_id=user_id,
                operation_id=operation_id,
                op="mcp_io",
                model_id=_MCP_IO_MODEL,
                units_in=Decimal(_estimate_tokens(request)),
                units_out=Decimal(_estimate_tokens(result)),
                basis=CostBasis.local,
            )
    except Exception:
        _log.warning("mcp_io metering failed for tool=%s", tool, exc_info=True)


# Tool args carried only for the legacy stdio flow; under HTTP/OAuth the
# principal comes from the bearer, so these are injected as empties at
# dispatch time and stripped from every schema the LLM sees.
_AUTH_PARAMS: tuple[str, ...] = ("token", "org_id")

#: Argument names that take a task or a note id, and which kind each one
#: takes. These are the two kinds ``lookup.resolve_prefix`` can expand, and
#: they are the same names ``_shorten_entity_ids`` writes short on the way out:
#: this surface has to accept back what it hands out, or a short id is a dead
#: end and the caller pays a ``resolve_prefix`` round trip to undo an economy.
#:
#: Expansion happens HERE and not in each tool because this is the single path
#: every concrete call takes, and because a tool that did its own would be the
#: 78th place in this package that turns a string into a uuid.
#: Derived by enumerating every ``@mcp.tool()`` argument that the tool body
#: passes to ``uuid.UUID(...)``, then keeping the ones whose value is a task or
#: a note. Re-derive it with:
#:
#:     grep -n 'uuid\.UUID(' mcp/src/mycelium_mcp/server.py
#:
#: The enumeration matters more than it looks. Two arguments were missed by
#: naming alone and were found only by a test failing: ``resource_id`` (below,
#: it is the capability tools' id) and ``seed`` (the note the note-graph walk
#: starts from), which reads like a parameter and is an id. Every other
#: uuid-parsed argument on this surface -- ``tag_id``, ``part_id``,
#: ``comment_id``, ``annotation_id``, ``blob_id``, ``revision_id``,
#: ``worker_id``, ``budget_id``, ``project_id``, ``user_id`` and the rest -- is
#: a kind ``resolve_prefix`` cannot expand, and those are exactly the ids this
#: surface never shortens either. The two halves agree by construction.
_PREFIX_ARGS: dict[str, tuple[str, ...]] = {
    "task_id": ("task",),
    "parent_task_id": ("task",),
    "predecessor_id": ("task",),
    "successor_id": ("task",),
    "other_id": ("task",),
    "note_id": ("note",),
    "parent_note_id": ("note",),
    "child_note_id": ("note",),
    "dst_note_id": ("note",),
    "src_note_id": ("note",),
    "source_note_id": ("note",),
    "target_note_id": ("note",),
    # The note a graph traversal starts from (``graph_walk`` /
    # ``graph_focus_context``), and the note the garden proposes about
    # (``garden_classify`` / ``garden_apply``).
    "seed": ("note",),
    "node_id": ("note",),
}

#: The same thing for arguments whose kind is decided by a SIBLING argument:
#: ``{arg: (discriminator_arg, {discriminator_value: kinds})}``. These are the
#: multiplexer tools, and they matter more than their number suggests --
#: ``get_text_block_capability(kind='task_description', resource_id=<task id>)``
#: is the token-free way to read a long description, which is precisely what
#: ``get_task`` now tells a caller holding a SHORT id to do. Without this entry
#: the surface would hand out a short id and then refuse it at the one door it
#: had just pointed at.
#:
#: A discriminator value that is absent from the inner map is left alone, which
#: is how ``kind='annotation'`` stays untouched: an annotation id cannot be
#: resolved from a prefix. A tool that has the argument but not the
#: discriminator (``add_annotation`` has ``parent_id`` under ``doc_kind``, and
#: its parent is another annotation) is left alone for the same reason.
_PREFIX_ARGS_BY_KIND: dict[str, tuple[str, dict[str, tuple[str, ...]]]] = {
    "resource_id": ("kind", {"task_description": ("task",)}),
    "parent_id": ("parent_kind", {"task": ("task",), "note": ("note",)}),
    # An annotation's document: a note PART under 'note_part' (not expandable,
    # so absent from the inner map) or a task under 'task_description'.
    "doc_id": ("doc_kind", {"task_description": ("task",)}),
}

#: A value this layer may expand: hex, dashes allowed, at least the ADR-0038
#: floor of 8 significant digits and shorter than a full uuid. A full uuid is
#: deliberately NOT matched -- it is passed through untouched, so nothing that
#: worked before this existed takes a different path now.
_PREFIX_RE = re.compile(r"^[0-9a-f]{8}[0-9a-f-]*$", re.IGNORECASE)
_FULL_UUID_LEN = 36
_SHORT_ID_LEN = 8

#: The top-level keys whose list holds rows of the tool's own entity.
#: ``open_tasks`` is here because ``whoami`` is the single worst payload to
#: miss: twelve calls in the whole recorded corpus carried 10% of this
#: server's context weight, because a bootstrap is read at turn 1 and then sits
#: in context for a thousand turns. It was missed by the first version of this
#: walk and found by reading a real payload rather than by a test.
_ROW_LIST_KEYS: frozenset[str] = frozenset({"items", "hits", "open_tasks"})

#: Tools whose result carries a task or a note under the bare key ``id``: at
#: the TOP level of the result, or in the rows of a top-level list named by
#: ``_ROW_LIST_KEYS``. Nothing deeper is touched, which is the rule that keeps
#: ``get_note``'s ``parts[].id`` (a part) and ``memory_search``'s
#: ``hits[].blob.id`` (a blob) full -- neither kind can be resolved back from a
#: prefix, and a short id nobody can expand is data loss rather than economy.
#:
#: A tool missing from this list keeps emitting full uuids, which still work
#: everywhere. That is the property that makes the list safe to be incomplete:
#: a miss costs tokens, never correctness.
_ENTITY_ROW_TOOLS: frozenset[str] = frozenset(
    {
        "whoami",
        "get_task",
        "list_tasks",
        "create_task",
        "task_pull",
        "task_claim",
        "task_offer",
        "task_decline",
        "get_note",
        "list_notes",
        "create_note",
        "get_or_create_task_note",
    }
)


def _short(value: Any) -> Any:
    """The short form of one id value, ADR-0038's 8 hex digits."""
    if isinstance(value, str) and len(value) == _FULL_UUID_LEN:
        return value[:_SHORT_ID_LEN]
    return value


def _shorten_entity_ids(result: Any, tool: str) -> Any:
    """Rewrite the task and note ids in a result to their short form.

    Why here and not in the serializers, which is where the entity kind is
    actually known: the short form is a property of THIS surface, and the
    other half of it -- accepting a short id back -- lives in
    ``_expand_prefixes`` a few lines below. A surface that hands out an
    identifier it cannot consume is broken, and the first version of this did
    exactly that: the serializers are shared with the stdio registry, which has
    no expansion, so ``create_task`` there returned an id that its own
    ``get_task`` refused. Keeping both halves in one module is what makes that
    mistake impossible rather than merely fixed.

    Two rules, and both are conservative on purpose:

    - a key in ``_PREFIX_ARGS`` (``task_id``, ``note_id``, ...) anywhere in the
      payload, because the key names the kind;
    - the bare ``id``, only at the top level or in the rows of a top-level
      ``_ROW_LIST_KEYS`` list, and only for a tool in ``_ENTITY_ROW_TOOLS``.

    Measured over 32 recorded sessions on 2026-09-17: a uuid costs ~23 tokens
    against ~4 for the prefix, uuids were 21% of every token this server
    returned, and 90% of the ones returned were never passed back to anything.

    What it does not cover: uniqueness. Two entities can share 8 hex digits,
    and this does not check. The guarantee is on the way back in --
    ``_expand_prefixes`` refuses an ambiguous prefix and names the candidates
    -- so a collision costs a round trip and never resolves to the wrong row.
    """

    def walk(node: Any, depth: int, row: bool) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for key, value in node.items():
                if key in _PREFIX_ARGS:
                    out[key] = _short(value)
                elif key == "id" and row and tool in _ENTITY_ROW_TOOLS:
                    out[key] = _short(value)
                elif key in _ROW_LIST_KEYS and depth == 0 and isinstance(value, list):
                    out[key] = [walk(v, depth + 1, True) for v in value]
                else:
                    out[key] = walk(value, depth + 1, False)
            return out
        if isinstance(node, list):
            return [walk(v, depth + 1, row) for v in node]
        return node

    return walk(result, 0, True)


# Coarse domain tags for the optional structural prefilter in
# ``search_tools``. Embeddings carry the real semantic match; tags are a
# cheap, deterministic narrowing (first matching rule wins, else "misc").
_DOMAIN_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Cross-cutting retrieval primitives. Listed FIRST and surfaced by
    # ``search_tools`` regardless of any ``domain`` prefilter: a discovery
    # goal scoped to e.g. domain='tasks' must still find ``search`` (the
    # only tool exposing a task tag / free-text facet) and its siblings.
    # Matches ``search`` / ``memory_search`` / ``graph_focus_context``.
    ("search", ("search", "focus_context")),
    # Session bootstrap / self-identity (whoami): a read-only "who am I / what
    # may I do / my durable memory" tool; its own small bucket.
    ("identity", ("whoami", "agent_home")),
    # Navigation/relations must come next: link/relation/resolve tools
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
            "text_block",
        ),
    ),
    ("billing", ("rate", "credit", "budget", "invoice", "usage", "meter")),
    ("email", ("email",)),
    ("taxonomy", ("tag", "client", "project")),
    ("tasks", ("task", "comment", "checklist", "item", "revision", "attachment")),
)

# Soft down-rank applied to a tool whose domain is not the one the caller
# asked for (the cross-cutting 'search' bucket is never penalized). A
# subtractive penalty, not a hard exclude (task 26efb287): the requested
# domain dominates the common case, yet a genuinely strong off-domain match
# (cosine gap > the penalty) can still surface below the in-domain hits
# instead of being hidden. Sized at 0.5 -- larger than any realistic
# off-domain advantage for an in-domain query, so a clear in-domain query
# still yields an all-in-domain top-k; small enough that a real
# cross-domain best-answer (e.g. an orchestration tool for a task-shaped
# query) beats a weak in-domain match.
_OFF_DOMAIN_PENALTY = 0.5

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


def _domain_penalty(requested: str | None, tool_domain: str) -> float:
    """Score penalty for an off-domain tool (0 when no domain was asked, the
    tool is in the requested domain, or it is a cross-cutting 'search' tool
    -- those always survive). See ``_OFF_DOMAIN_PENALTY``."""
    if requested is None or tool_domain == requested or tool_domain == "search":
        return 0.0
    return _OFF_DOMAIN_PENALTY


def _summary(description: str | None) -> str:
    if not description:
        return ""
    for line in description.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _searchable_text(name: str, domain: str, summary: str) -> str:
    # The domain prefix gives the embedder a categorical signal the
    # terse per-tool docstrings lack (the article's "categorical
    # overview" trick), improving discoverability without editing every
    # docstring. The name is de-snaked (list_tasks -> "list tasks") so
    # its words are real tokens for both the model and the lexical
    # fallback. We rank on the one-line SUMMARY, not the full docstring:
    # a long docstring (e.g. list_tasks' filter/sort/pagination prose)
    # otherwise dilutes the name + topic signal under bag-of-words /
    # length-sensitive embeddings and buries the tool for an obvious
    # query. The summary is the curated, length-stable signal.
    return f"[{domain}] {name.replace('_', ' ')}: {summary}"


def _catalog() -> list[dict[str, Any]]:
    global _catalog_cache
    if _catalog_cache is None:
        cat: list[dict[str, Any]] = []
        for t in _registry._tool_manager.list_tools():
            domain = _domain_for(t.name)
            summary = _summary(t.description)
            cat.append(
                {
                    "name": t.name,
                    "summary": summary,
                    "domain": domain,
                    "text": _searchable_text(t.name, domain, summary),
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
            results = await embed_batch(emb, [m["text"] for m in cat], side=EmbedSide.document)
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
    """Liveness probe; returns the mycelium-core version."""
    return f"mycelium-core {__version__}"


@gateway.tool()
async def search_tools(
    query: str, limit: int = 8, domain: str | None = None
) -> list[dict[str, Any]]:
    """Find the concrete Mycelium tools relevant to a natural-language goal.

    Returns ranked ``{name, summary, domain}`` entries (most relevant
    first). This is
    the entry point of the dynamic-toolset flow: search here, then call
    ``describe_tools`` for the schemas of the ones you want, then
    ``execute_tool`` to run them. ``domain`` optionally biases ranking
    toward one of: tasks, notes, search, navigation, time, calendar,
    memory, orchestration, workflow, taxonomy, billing, email, misc. It is
    a SOFT down-rank, not a hard filter: off-domain tools are demoted, not
    removed, so the requested domain dominates the top results while a
    genuinely strong off-domain match can still surface below them. The
    cross-cutting search tools (``search`` / ``memory_search`` /
    ``graph_focus_context``) are never penalized, so a domain-scoped query
    always reaches them.
    """
    cat = {m["name"]: m for m in _catalog()}
    # Every tool is a candidate; ``domain`` only down-ranks off-domain tools
    # (task 26efb287). The old hard prefilter hid a strong cross-domain
    # answer outright; the penalty keeps it reachable while the requested
    # domain still wins the common case.
    #
    # Scope filter (task c19f2f63, enabler B): a scoped assistant never sees a
    # tool it could not call -- defence-in-depth over the ``execute_tool`` gate
    # and better ergonomics (no dead search hits). Full-access callers keep the
    # whole catalog.
    names = [n for n in cat if _scope_permits(n)]
    if embedder_available():
        await _ensure_index()
        index = _index or {}
        qv = _normalize((await get_embedder().embed(query, side=EmbedSide.query)).vector)
        scored = [
            (_cosine(qv, index[n]) - _domain_penalty(domain, cat[n]["domain"]), n)
            for n in names
            if n in index
        ]
    else:
        qtok = set(query.lower().split())
        scored = [
            (_lexical(qtok, cat[n]["text"]) - _domain_penalty(domain, cat[n]["domain"]), n)
            for n in names
        ]
    scored.sort(key=lambda s: s[0], reverse=True)
    out: list[dict[str, Any]] = []
    # Emit in rank order (most relevant first); the numeric ``score`` is
    # dropped from the payload — the LLM acts on the ordering and the
    # summary, never on the float, so it was pure token overhead.
    for _score, name in scored[: max(1, limit)]:
        m = cat[name]
        out.append({"name": name, "summary": m["summary"], "domain": m["domain"]})
    _record("search", "search_tools", out)
    await _meter_io("search_tools", {"query": query, "limit": limit, "domain": domain}, out)
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
        if not _scope_permits(name):
            # Don't hand out the schema of a tool this assistant can't call
            # (task c19f2f63, enabler B). Same two readings as the execute
            # envelope: "scope denied" invites a request for a bigger scope,
            # which for a HUMAN_ONLY tool is a request nobody can grant.
            out.append(
                {
                    "name": name,
                    "error": (
                        "not callable by an assistant credential; a person can do it"
                        if TOOL_SCOPES.get(name) is HUMAN_ONLY
                        else "scope denied; not permitted by this assistant's scope"
                    ),
                }
            )
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
    _record("describe", "describe_tools", out)
    await _meter_io("describe_tools", {"names": names, "minimal": minimal}, out)
    return out


async def execute_tool(name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Run a concrete Mycelium tool by name with ``arguments`` (the schema
    from ``describe_tools``, minus the auth args). token/org_id are
    injected from the authenticated principal, so omit them.

    Failures come back as a structured ``{"error": {...}}`` envelope, not
    an opaque string, so the caller can branch on a code instead of
    pattern-matching prose:

    - unknown tool name -> ``{"error": "unknown tool: ...; call search_tools first"}``
    - wrong/missing/extra arguments -> ``{"error": {"code": "invalid_arguments",
      "detail", "tool", "hint"}}`` pointing back at ``describe_tools`` for
      the schema (args are validated against the signature *before* the
      tool runs, so a typo never surfaces as a raw Python ``TypeError``)
    - a domain/validation failure -> ``{"error": {"code", "detail", "params"}}``
      mirroring the HTTP adapter's envelope: ``code`` is the stable
      ``MessageCode`` (e.g. ``note.link.kind_invalid``) and ``params``
      carries the constraint context (valid values, limits)."""
    tool = _registry._tool_manager.get_tool(name)
    if tool is None:
        return {"error": f"unknown tool: {name}; call search_tools first"}
    # Per-tool scope gate (task c19f2f63, enabler B). ``execute_tool`` is the
    # sole path to a concrete tool over the HTTP transport, so this is the
    # security chokepoint. Deny BEFORE validating args / running, so a forbidden
    # call leaks neither the arg schema nor any side effect. Full-access callers
    # (bare token / stdio / human) pass through unchanged. The concrete args are
    # passed so an argument-dependent tool (DYNAMIC_TOOL_SCOPES, e.g. the
    # text_block / list_attachments kind multiplexers) is gated on the scope its
    # SPECIFIC call needs, not a single coarse key.
    args = dict(arguments or {})
    if not _scope_permits(name, args):
        req = required_scope_for_call(name, args)
        # Two refusals that must not read the same, because they imply
        # opposite next actions. "your scope is too small" tells an agent to
        # ask for a key; HUMAN_ONLY means no key exists, so the same wording
        # would send it to ask for something nobody can grant, and it would
        # ask again.
        #
        # ``req`` is also not JSON-serialisable here: it is a sentinel, so a
        # branch that only handled UNMAPPED would put an enum member in the
        # envelope. That is the failure mode of a refusal path -- it is the
        # path nobody exercises until it matters.
        if req is HUMAN_ONLY:
            return {
                "error": {
                    "code": MessageCode.MCP_SCOPE_DENIED.value,
                    "detail": (
                        "this tool is not callable by an assistant credential; "
                        "a person can do it from the app"
                    ),
                    "tool": name,
                    "required_scope": None,
                    "scope_would_not_help": True,
                }
            }
        return {
            "error": {
                "code": MessageCode.MCP_SCOPE_DENIED.value,
                "detail": "this assistant's scope does not permit this tool",
                "tool": name,
                "required_scope": req if req is not UNMAPPED else None,
            }
        }
    # Short ids are expanded here, AFTER the scope gate on purpose: a caller
    # that may not run this tool must not learn from the refusal whether an id
    # exists, which is the same reasoning that puts the gate before argument
    # validation.
    prefix_error = await _expand_prefixes(args)
    if prefix_error is not None:
        return prefix_error
    props = (tool.parameters or {}).get("properties", {})
    for p in _AUTH_PARAMS:
        if p in props:
            args.setdefault(p, "")
    # Validate the call shape against the real signature BEFORE running:
    # a wrong/missing/extra argument otherwise leaks as a raw Python
    # ``TypeError`` from ``fn(**args)``. Point the caller at describe_tools
    # (the schema), not at search_tools (names only). ``signature`` can
    # fail on exotic callables -> skip the precheck rather than block.
    try:
        sig: inspect.Signature | None = inspect.signature(tool.fn)
    except (TypeError, ValueError):
        sig = None
    if sig is not None:
        try:
            sig.bind(**args)
        except TypeError as exc:
            return {
                "error": {
                    "code": "invalid_arguments",
                    "detail": str(exc),
                    "tool": name,
                    "hint": f"call describe_tools(['{name}']) for the input schema",
                }
            }
    try:
        result = tool.fn(**args)
        if tool.is_async:
            result = await result
        # Only on the success path: the refusals above carry full uuids on
        # purpose (an ambiguous prefix is answered with the candidates' whole
        # ids, which is the one thing that lets the caller get unstuck).
        result = _shorten_entity_ids(result, name)
        _record("execute", name, result)
        await _meter_io("execute_tool", {"name": name, "arguments": arguments}, result)
        return result
    except DomainError as exc:
        return {
            "error": {
                "code": exc.code.value,
                "detail": str(exc),
                "params": jsonable_params(exc.params),
            }
        }


# The registered tool is this adapter, not ``execute_tool`` itself: the
# published surface owes the client a wire format, while the function owes its
# callers (the test suite, the eval scenarios) a Python value. Splitting them
# is what lets the wire format change without rewriting every assertion that
# reads a field off a result. The signature is the one the LLM sees, so it
# mirrors ``execute_tool``'s exactly; the description is taken verbatim from
# it so there is one text and not two that drift.
#
# ``structured_output=False`` is explicit rather than inferred: a ContentBlock
# return happens to suppress the output schema today, and this call is what
# makes the payload-sent-once property a decision instead of an accident.
@gateway.tool(
    name="execute_tool",
    description=execute_tool.__doc__ or "",
    structured_output=False,
)
async def _execute_tool_wire(name: str, arguments: dict[str, Any] | None = None) -> TextContent:
    return _wire(await execute_tool(name, arguments))


__all__ = ["describe_tools", "execute_tool", "gateway", "ping", "prewarm", "search_tools"]
