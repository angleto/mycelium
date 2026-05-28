# ADR-0036 — Event bus for multi-agent coordination on the graph

Status: Proposed
Date: 2026-05-27
Tracks: task `f1801a47-eefd-484d-9c25-241fb42eefed`
Depends on: ADR-0025 (executor registry), ADR-0027 (adjudication), ADR-0028 (identity-first addressing)

## Context

Multi-agent operation on the garden is already possible in practice:
the SPA, the CLI, the Neovim plugin, the Telegram bot and an
arbitrary MCP client can all mutate the same workspace through the
existing REST surface. What is missing is a *coordinated* read/write
substrate: agents cannot observe each other, an LLM's proposal
cannot enter an adjudication queue without bespoke plumbing, and
audit of "who proposed what when" is scattered across per-table
revision logs.

The bus is the spine that lets multiple actors (human + agent) cohabit
the same graph without stepping on each other.

## Decision

### Transport: Postgres LISTEN/NOTIFY + outbox

- **Primary**: an `event_outbox` table written in the same
  transaction as the originating mutation. Authoritative state.
- **Notification**: Postgres `pg_notify('flow.event', '<event_id>')`
  in a deferred trigger. Subscribers `LISTEN flow.event` and pull
  the row by id.
- No Redis, no Kafka, no broker. RLS already protects the outbox
  the same way it protects every other table; reuse the security
  story instead of inventing another.

### Event schema

```sql
CREATE TABLE event_outbox (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id        uuid NOT NULL,
  actor_id      uuid NOT NULL,
  actor_kind    text NOT NULL CHECK (actor_kind IN ('human','agent','system')),
  kind          text NOT NULL CHECK (kind IN ('read','propose','commit','reject','snapshot')),
  node_kind     text,                  -- 'note' | 'task' | 'note_part' | 'tag' | 'edge'
  node_id       uuid,
  parent_event_id uuid REFERENCES event_outbox(id),
  payload       jsonb NOT NULL,
  ts            timestamptz NOT NULL DEFAULT now(),
  applied_at    timestamptz,           -- null until adjudicator decides
  applied_state text                   -- 'committed' | 'rejected' | 'merged'
);
CREATE INDEX ON event_outbox (org_id, ts DESC);
CREATE INDEX ON event_outbox (org_id, node_id, ts DESC);
```

`payload` carries the proposed mutation in a kind-specific schema
(separate `payload_schema_version` field for future evolution).
`parent_event_id` chains propose → adjudication → commit so the
adjudicator's verdict is itself an event.

### Ordering & idempotency

- Per `(org_id, node_id)` strict order via `ts` and event id.
- Producers MUST send an `Idempotency-Key`; the bus dedupes inside a
  24h window.
- Commit events are idempotent at apply time (services check that
  the target row has not advanced past the `parent_event_id` already
  applied).

### Adjudication policy

- `propose` events feed `adjudication_queue` (ADR-0027). The queue
  decides between single-shot apply, debate, or human-in-loop based
  on the actor's trust level (executor registry, ADR-0025) and the
  payload severity (low for tag suggestion, high for delete).
- The adjudicator emits a `commit` or `reject` event referencing the
  original `propose` via `parent_event_id`.

### Agent registry & quotas

- The executor registry (ADR-0025) already lists authorised agents.
  Each row gets two new columns: `event_quota_per_min` and
  `event_quota_per_day`. The gateway counts events at insert time
  and 429s past quota.
- Human actors share the org's default quota.

### Subscribers

Three first-class consumers:

1. The learning loop (ADR-0037) ingests commit/reject events to
   update priors.
2. The SPA's mindmap subscribes to commit events scoped to the
   current workspace and refreshes the affected nodes.
3. The audit panel (`/garden/audit`) renders the event stream
   verbatim.

External MCP agents subscribe via a future
`/agents/events/stream` SSE endpoint; out of scope for this ADR.

### Guarantees

- **No silent loss**: outbox row is the source of truth; subscribers
  can replay from `(org_id, ts > cursor)` at any time.
- **At-least-once delivery**: subscribers must be idempotent on
  event id.
- **No total ordering across orgs**: each tenant gets its own
  totally-ordered stream; cross-tenant interleaving is undefined.

## Consequences

- One audit story replaces the per-table revision tables for events
  that flow through the bus. The legacy revision tables remain for
  inline edits that bypass propose/commit (e.g. autosave drafts).
- LISTEN/NOTIFY has a 8 KB payload cap; we work around by sending
  only the event id and letting subscribers `SELECT` the row.
- Backpressure: a misbehaving subscriber that falls behind cannot
  block producers (outbox is durable); the SSE endpoint must drop
  the slowest readers rather than buffer unboundedly.
- The adjudicator becomes more central: a regression in
  ADR-0027 stalls the propose → commit pipeline. Mitigation: an
  explicit `auto_commit` policy for trusted actors so the
  adjudicator is not in the hot path of low-risk events.

## Alternatives rejected

- **Redis streams.** Loses the "same RLS, same DB, same transaction"
  story. Extra moving piece for a single-tenant deploy.
- **No bus; expose REST endpoints per concern.** Today's status,
  and it does not scale to "many agents on the same node".
- **Synchronous mutex on the node.** Kills parallelism; the
  adjudicator's job is already to serialise the *meaningful*
  propose-commit edges.

## Resolved questions

**Should `read` events be persisted?** Persist `read` events only for
non-human actors (`actor_kind in ('agent','system')`); never for
`actor_kind='human'`. Agent reads are an audit need ("agent X
classified this node yesterday") and carry no chilling effect; human
reads are PII-adjacent surveillance with no offsetting benefit. The
outbox trigger drops human `read` events before insert. Agent `read`
rows follow the same retention as the rest of the outbox (no special
TTL); ADR-0037 reads commit/reject only, so this choice does not
affect the learning loop.
