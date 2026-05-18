# ADR-0002 Multi-tenant: optimistic concurrency, mandatory RLS

Status: accepted. Corrects a wrong choice in an earlier draft.

## Context

An earlier draft prescribed both "last-write-wins" **and** "version
(optimistic concurrency)" at once. These are opposite conflict
policies: LWW accepts the stale write by overwriting (lost update
accepted); optimistic concurrency rejects the stale write (lost update
prevented). They cannot coexist on the same path. The draft also marked
RLS as "optional": for a multi-tenant system holding email and fiscal
data, leaving isolation to query diligence alone is a security
regression.

## Decision

Optimistic concurrency **only**: `UPDATE ... WHERE id = ? AND
version = ?`; 0 rows -> `409 Conflict` propagated to GUI/REST/MCP
(enforced in the service layer). Append-only activity log. Realtime
invalidation via WebSocket. No silent lost update. **Mandatory RLS**
on every org-scoped entity as the primary defense, not optional.

## Consequences

- The model is fit to introduce collaboration later without redesigning
  the core (LWW would do the opposite).
- Derived rows (e.g. `schedule`) are not subject to user optimistic
  concurrency: the most recent recompute wins.

## Alternatives rejected

- Last-write-wins: introduces structural lost updates that any future
  collaborative feature would then have to undo.
- Optional RLS: a single forgotten predicate = cross-tenant leak.
