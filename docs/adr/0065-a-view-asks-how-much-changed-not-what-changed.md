# ADR-0065: A live view asks how much changed, not what changed

Status: Accepted (2026-09-19)
Relates to: ADR-0002 (the append-only activity log this reads, and the
optimistic concurrency that decides what a refresh may overwrite: nothing),
ADR-0036 (the agent event bus, whose `pg_notify` transport is the push this
deliberately does not build yet), ADR-0001 (two thin adapters over one
domain: the refresh goes back through the adapter, not around it).

## Context

A browser on `/tasks` renders the snapshot it fetched when it mounted. The
workspace it is looking at is written to continuously by MCP sessions, the
CLI and the worker, so that snapshot is wrong within seconds and nothing
tells it. The existing `useStaleWatch` covers one open entity, compares a
version on focus, and offers a banner; a list and a board have no version,
no single entity, and nothing unsaved to protect, so none of it transfers.

Two shapes were on the table. A pushed stream (WebSocket or SSE, fed by the
`pg_notify` channel ADR-0036 already installs), and a polled delta.

Three measurements decided it, all taken before the first line was written.

**A browser WebSocket cannot carry this SPA's credential.** The client
authenticates with an `Authorization: Bearer` header injected in an
openapi-fetch middleware; the browser `WebSocket` constructor cannot set
headers, and a token in the query string is refused outright. That leaves a
cookie (a second authentication mechanism beside the one we have) or the
`Sec-WebSocket-Protocol` smuggle. SSE consumed through `fetch` keeps the
header and the existing 401-refresh path, so a WebSocket was out before the
scale question was reached.

**The probe is free, and only with the entity enumeration.** Measured on
200k `activity_log` rows in one org: `max/count` filtered on org plus an
enumerated set of entity kinds is an index-only scan of **0.22 ms** over
`ix_activity_log_org_entity_ts`; the same query without the kinds is a
parallel sequential scan of **25.8 ms** that grows with the workspace's
history, because the index leads on `entity`. Ten browsers polling every
five seconds are two queries a second.

**`tasks.updated_at` is not the signal.** Attaching a tag or a collaborator
writes a junction row and an audit row and never touches the task row, and
both are drawn on the list row. A delta keyed on the task table misses
exactly the edits an agent makes most, and misses them silently.

## Decision

A view asks **how much** has changed, never **what** changed.

`GET /activity/watermark?scope=<scope>&since=<instant>` answers with a count
and with the instant the server measured from. The count moving is the whole
signal; the view then re-reads itself through the fetch it already performs
on mount, so authorisation, redaction and tenancy apply to the refresh
exactly as to the first load. No data travels on the watching path.

- **The source is the activity log**, not the entity tables, because the
  audit row is written by the same choke point that made the change whatever
  table it touched, and because it is already indexed for this question.
- **The scope is a closed set** (`WatchScope`), and it decides which index
  range is read. An unrecognised scope is refused, not widened.
- **The instant is the server's.** The client never invents a timestamp; it
  echoes back the one it was given. The bootstrap deliberately starts
  `BOOTSTRAP_OVERLAP` in the past, because `ts` is the writing transaction's
  start time and a write already in flight carries a timestamp older than
  the instant being handed out. The consequence is that a fresh watch starts
  with a non-zero count, which is why the client compares against its
  baseline rather than against zero.
- **The re-baseline happens before the re-read**, never after: a write
  landing between the two would otherwise be counted into the new baseline
  while missing from the data just fetched. The cost of that order is at
  most one redundant refresh.
- **The transport is a seam.** The hook takes a probe function; the views
  subscribe to "something changed", not to a socket.

## Consequences

- Latency is bounded by the poll interval (5 s) rather than by the network.
  A change also lands on the next tab focus, which covers a backgrounded
  window.
- The expensive read happens only when something moved. Measured against the
  running app: 20 idle seconds cost 4 probes and **zero** list reads.
- The probe over-triggers by design: any watched entity kind moving in the
  workspace refreshes the view, including one the current filter excludes.
  Over-triggering is a wasted fetch; under-triggering is the bug this
  exists to prevent.
- A refresh that lands mid-gesture would lose it, so the watch stands down
  while a drag is in flight, while a `<select>` has focus, and while the tab
  is hidden.
- REST-only on purpose, recorded in `docs/mcp-coverage.md`: an agent reads
  the task at the moment it acts and holds no rendered snapshot to
  invalidate.
- The window a client counts over starts at its last re-baseline, so it
  stays small exactly when the workspace is busy.

## Alternatives rejected

**A WebSocket.** Ruled out by the credential, above, before latency was
weighed.

**SSE over fetch, fed by `LISTEN mycelium.event`.** Technically sound and
the right answer at a scale this deployment is nowhere near. It costs a
surface in the authorization map, a grant that outlives the token that
opened it (so the server must close the stream at `exp`), a declared bound
on concurrent streams, a per-replica connection registry to enumerate as
stateful, drain on shutdown, an end-to-end test that needs a socket, and an
endpoint no generated type can describe. It buys 5 seconds. Revisit when a
workspace holds tens of browsers, or when two people drag the same board:
the seam is the hook's `probe`.

**Re-fetching `GET /tasks?updated_since=…` and merging.** The parameter
exists, and it is the wrong signal (above). Merging would also be a second
implementation of the view's state beside the one the route already has.

**A cursor that cannot miss a late commit** (a snapshot `xmin` rather than a
clock). Correct, and larger than this feature: transactions on this path are
single-statement, so the overlap covers them. What the overlap does not
cover is written down where the constant lives.
