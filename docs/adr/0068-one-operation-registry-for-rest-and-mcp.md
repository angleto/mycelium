# ADR-0068: One operation registry, bound by REST and MCP

Status: Accepted (2026-11-13); the garden is the first domain on it
Relates to: ADR-0043 and ADR-0066 (operations a person decides; the registry
carries that property instead of two scope tables repeating it); ADR-0064
(the cheap default shape of an MCP result, which the `lean` projection
below makes declarable per entity).

## Context

Every capability is declared twice: once as a FastAPI route
(`api/src/mycelium_api/routers/`) and once as an MCP tool
(`mcp/src/mycelium_mcp/server.py`). Each copy picks its own scope, parameter
names, pagination and response shape by hand, and both call the same core
service. A static measurement of the tree on 2026-11-12 (AST over every route
and tool, matching on the core service function each one calls):

- **Parity is not true and is not checked.** 472 routes, 287 tools. 283
  routes have an MCP equivalent. Of the other 189, 122 are deliberate
  (person-only, public, plumbing), 41 are not yet classified, and 26 are gaps
  an agent needs, among them the garden's
  health sensors, the per-model accept ratio, the classifier's learning
  telemetry, and correcting an invoice draft. `docs/mcp-coverage.md` states
  that every capability has a REST sibling and vice versa; the one test that
  compares the surfaces checks 8 pairs.
- **There is no shared read model.** `server.py` holds 61 hand-written
  serialisers and 164 of 287 tools return dict literals; REST has 151 `*Out`
  schemas. They drift: `get_tag` cannot read back the colour and status
  `update_tag` writes; an invoice has 14 fields over MCP and 32 over REST;
  `list_notes` has two different contracts.
- **Conventions vary per operation.** `expected_version` is on 19 of 32
  update tools; 30 of 46 list tools have no pagination; names are verb-first
  197 times and noun-first 39 times.
- **Errors leak.** Tools parse their own arguments (328 `uuid.UUID(...)`
  calls), and `execute_tool` calls the function directly, past FastMCP's
  argument validation, so a malformed id reaches the client as text with no
  code.
- **Discovery guesses.** `search_tools` assigns a tool's domain from
  substrings of its name; 40 tools land in "misc".

Adding a capability touches about seven places, and nothing fails when one of
them is forgotten. That is the cause of all of the above, and fixing each
symptom by hand would re-create it with the next capability.

## Decision

Declare each operation **once**, in core, and generate both transports from
it.

```python
# core/src/mycelium_core/operations/garden.py
@operation(
    "garden.review.approve",
    domain="garden",
    kind=Kind.WRITE,              # READ | WRITE | DESTRUCTIVE
    access=HUMAN_ONLY,            # a scope key, an any-of set, HUMAN_ONLY or META
    params=GardenReviewActionIn,  # typed: UUID, enums, bounds, expected_version
    result=GardenReviewActionOut, # core read model
)
async def review_approve(ctx: OpCtx, p: GardenReviewActionIn) -> GardenReviewActionOut:
    """The one description both surfaces publish."""
```

```python
# api/src/mycelium_api/routers/garden.py
("POST", "/review/approve", "garden.review.approve", "garden_review_approve"),
# mcp/src/mycelium_mcp/tools_garden.py
("garden_review_approve", "garden.review.approve", None),
```

1. **The registry is the source of truth.** An operation declares its id,
   domain, kind, access, argument model and read model; its docstring is the
   description both surfaces publish. An operation that is deliberately on
   one surface says so in the declaration (`only=` with a written `why=`),
   where a reviewer reads it. Declarations are checked at import: an unknown
   domain or scope key, a duplicate id, a one-surface operation without a
   reason.
2. **Transports bind, they do not declare.** A route names its verb, path,
   route name and operation; a tool names its tool name, operation, and
   optionally an envelope (`{"pending": [...]}`) its existing readers expect,
   a default of its own for an argument its readers usually want the other
   way, and a lean answer (null, false and empty fields left out, which the
   tool's description then says).
   The route's path, query or body parameters and the tool's input schema are
   generated from the argument model, so the bounds and descriptions are the
   same on both. The adapter keeps only transport concerns: how the
   principal is authenticated and the session opened.
3. **Scope entries are written by the binding.** Binding a tool writes its
   `TOOL_SCOPES` entry from `access`; building the app writes the
   `ROUTE_SCOPES` entry of every bound route. A hand-written entry for a
   bound name is refused at import, so the two declarations cannot disagree.
   Unbound routes and tools keep their hand-written entries until their
   domain migrates.
4. **Read models live in core.** One model per entity, served by both
   surfaces (`mycelium_core.operations.garden_views` for the garden). The
   class names did not change, so the OpenAPI component names and the web
   client's generated types kept theirs. A smaller MCP projection, where one
   is wanted, is a named model, not a hand-built dict.
5. **Arguments are validated once.** The argument model parses ids, enums
   and bounds; a failure is `operation.invalid_argument`, a 422 on REST and
   an `{"error": {"code": ...}}` on MCP, raised before any session is
   opened. `execute_tool`'s signature check still rejects an unknown
   argument name first.
6. **Discovery reads the registry.** `search_tools` takes a bound tool's
   domain from its operation, and the MCP tool annotations (`readOnlyHint`,
   `destructiveHint`) come from `kind`.
7. **Parity is a test, total from the first day.**
   `api/tests/test_operation_parity.py` checks that every operation is bound
   on every surface it does not decline, that each binding serves the
   operation's arguments, read model, scope and kind, and that every route
   and tool that binds nothing is listed in `UNBOUND_ROUTES` /
   `UNBOUND_TOOLS`. Those lists must equal what is actually unbound: a new
   route or tool binds an operation or is added to them in the same diff,
   where review sees it, and an entry that gets bound must be deleted. They
   can still be extended by editing the test; that is visible, not
   prevented. `KNOWN_GAPS` holds operations missing from a surface they
   should be on; it is empty for the garden.

## Migration

One domain at a time, behaviour unchanged for clients except where a
surface gains what the other already had:

1. **Done (2026-11-13).** The registry, the binders, the scope entries
   written by them, the parity test, with 456 routes and 274 tools recorded
   as unbound.
2. **Garden: done (2026-11-13).** 22 operations, bound by 21 routes and 20
   tools. Closed gaps: on MCP the health sensors, their history and events,
   the clusters, the per-model accept ratio, the learning telemetry and its
   rollback (seven new tools); on REST the two decision-carrying review
   routes. Drift found and resolved by choosing the fuller side:
   `garden_classify` now serves the persisted suggestions and the caller's
   learned priors as the route did; the route's link suggestions now carry
   the title the tool had; the tool's candidates honour the project
   perimeter; `list_events` and `GET /garden/audit` are one operation
   accepting both `since` and `days`; the review actions answer the same
   fields on both. Two pairs stayed apart, declared one-surface with their
   reason: the whole-graph dump (`garden.graph`, REST) and the walk (a
   trajectory with revisits for the mindmap on REST, a de-duplicated reading
   set with titles on MCP).
3. **Notes: reading done (2026-11-13), writing to follow.** `notes.get`,
   `notes.parts.list` and `notes.list` serve `GET /notes/{id}`,
   `GET /notes/{id}/parts`, `GET /notes`, `get_note`, `list_note_parts` and
   `list_notes` from one `NoteOut` and one `NotesPage`. Each surface gained
   the other's fields (REST: who wrote each part and when, the note's
   timestamps and review state, an outline on request; MCP: the task title,
   the generated-task links, the list preview). Each keeps its own default
   where its readers want the other value: MCP leaves out the joined
   `transcript` and lists parts as an outline. The MCP answer is lean, null,
   false and empty fields left out, the page envelope kept whole (measured:
   a six-part note 6,099 to 5,361 characters, its outline 3,337 to 2,761;
   twenty list rows 7,958 to 7,628).

   **The list is paged on both surfaces.** `GET /notes` answered the 200
   newest notes in one list, and the SPA's twelve callers took that for the
   whole workspace: pickers could not find an older note, the bin missed
   every note deleted before the newest 200, the garden map left nodes out.
   It now answers `{items, next_cursor, truncated}` with the tool's filters
   plus `ids`, `only` (deleted or archived) and `any_tag_ids` (a client or
   project focus), all applied in SQL before paging. The SPA asks
   for what each screen shows: the notes list and the bin a page at a time
   with "load more"; pickers and mentions search the server as the person
   types; panels label the notes they know by id; the garden map reads every
   page. The writing tools still build their answer with the old
   hand-written serialiser; they move with the note writes, the slice that
   touches the person's text and so goes last and separately.
4. Then tasks, annotations, invoicing, and the rest, each closing its
   gaps, moving its serialisers to core read models and removing its entries
   from the unbound lists.

Per domain, the step is done when its routes and tools are bound, its entries
are gone from both lists, and its existing tests pass unchanged.

## Consequences

- Adding a capability is one declaration plus two one-line bindings, and
  forgetting a binding fails the parity test.
- Error codes change where validation moved: a malformed argument to a bound
  tool is `operation.invalid_argument` rather than the tool's own code (for
  instance `garden.action_invalid` for an `auto` action, or a raw parse
  error for a bad id), and an out-of-range `limit` is refused rather than
  clamped.
- REST response shapes may gain fields that MCP had and vice versa; where a
  projection is deliberately smaller (MCP list rows), it is named and tested
  as a subset of the full model, not hand-built.
- `server.py` stops being one 10,500-line module: tools move next to their
  operations' domain.
- The legacy `token` / `org_id` parameters are still in the MCP schemas of
  bound tools. The HTTP gateway already injects them; the stdio entry point
  (`main.py`) serves the raw tools and takes them from the call, so it has to
  move onto the same dispatch before they can go.

## Rejected

- **Close the 26 gaps by hand.** Each fix is correct and the next capability
  re-opens the problem: the count of hand-maintained places stays at seven.
- **Generate REST routes from MCP tools, or the reverse.** Either surface
  would carry the other's transport decisions (paths, HTTP semantics, MCP
  naming) and the core would still not own the contract.
- **A parity test without a registry.** It can only compare names or
  scopes; it cannot tell that two surfaces return different fields for the
  same entity, which is where most of the measured drift is.
