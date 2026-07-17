# ADR-0049 — Working memory is delegated to the calling agent

Status: Accepted (2026-07-17)
Task: 68052297 (from the memory-system audit, note `bdc62d7a` §1a/§7.4 —
recording a boundary that was real but undocumented).
Relates to: ADR-0016 (retrieval-as-a-plan), ADR-0043 D2 (one effective
predicate on every surface), the MCP dynamic-toolset gateway.

## Context

The canonical agent-memory stacks include a "working memory" layer:
session-scoped state, salience, pinning, scratch space that resets when
the session ends. mycelium has none of that on the server, and no ADR
said whether that was a boundary or a hole. The audit confirmed the
absence is systematic — nothing memory-relevant keys on a session:
salience fields are global cumulative (`access_count`, `access_score`),
there is no pinning, no TTL scratch namespace, no session entity — and
that what serves the working-memory *function* is a consistent family of
per-request, budgeted constructors: `graph_focus_context` (PPR reading
set, budget + snippets), `bounded_neighborhood` (node + char budgets
with `stopped_by` attribution), retrieval `limit` + `RetrievalMeta`, and
the MCP gateway's dynamic toolset (three meta-tools instead of the full
registry, precisely to spare the caller's context window).

## Decision

**Working memory belongs to the calling agent, not to mycelium.** The
server's contract is: serve high-fidelity UNITS under explicit budgets,
with enough metadata (scores, snippets, provenance, budget attribution)
for the caller to manage its own context window. The caller — an LLM
harness that alone knows its window, its task and its attention — holds
the session; mycelium holds the substrate. Attention is not stored: it
is observed post-hoc (activity log, retrieval traces) and composted into
durable structure (coactivity and usage edge weights), which is the
system's inversion of the canonical layer rather than a missing piece.

Consequences of drawing the line here:

- No session-scoped salience, pinning or TTL scratch will be added to
  the blob store; a "pinned for this conversation" concern is the
  caller's prompt/context policy, not a server flag.
- Per-request constructors keep budgets EXPLICIT in their signatures
  (node/char/limit) — the server never silently decides how much
  context the caller can afford.
- What this boundary does NOT settle (open, tracked separately): a
  *shareable* working-set object for multi-agent coordination (today
  agents coordinate through durable artifacts under optimistic
  concurrency, and the event bus is not exposed over MCP). If that
  lands, it lands as a first-class durable entity with provenance and
  RLS — not as server-side session state.

## Alternatives rejected

- **A server-side session/working-memory store** (Letta-style core
  blocks). Rejected: it fuses agent state with the memory substrate,
  couples mycelium to one caller's context policy, and re-creates the
  state the stateless MCP/REST surface deliberately avoids. The
  economics that justify memory-as-agent (thin callers, long autonomous
  loops) are not this system's profile (contratto funzionale, nota
  `9a2adb4a` §1).
- **Leaving the boundary undocumented.** That was the bug: by the
  project's own discipline an absence without an ADR is a gap, not a
  choice. This ADR is the record.
