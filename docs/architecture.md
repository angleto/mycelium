# Architecture

Python monorepo + TypeScript frontend.

## Components

- `core/`: domain + the **single service layer** (business logic, RBAC,
  state machine, scheduler, advisory planning engine (feasibility +
  ranking + constrained selection), budgets, memory engine, SDI XML
  generation/validation, mandate and conservation). Single source of
  truth.
- `api/`: FastAPI, REST + WebSocket. Thin adapter over the service
  layer.
- `mcp/`: MCP server (Python SDK). Thin adapter, co-equal to `api/`.
- `web/`: React/TS SPA (lists/board/calendar, graph, Gantt, email
  triage, invoices, reports). Types generated from OpenAPI.
- `worker/`: jobs (IMAP sync, scheduler, memory/promotion/re-embedding,
  recurrences, reminders).
- `sdi-inbound/`: an always-on SOAP service with mutual TLS for push
  SdI notifications (not a polling worker).
- `db`: PostgreSQL with `pgvector`. SQLAlchemy + Alembic. `org_id`
  everywhere, mandatory RLS, memory partitioned by `org_id`.
- `cache/broker`: Redis (job queue, pub/sub for WebSocket).
- connectors: Gmail OAuth2; Proton Bridge sidecar (arm64); generic
  IMAP; `SdiChannel`; `ConservationProvider`; `Embedder`/`LLMProvider`
  (bitvision_phoenix pattern).

## Diagram

```
 Claude → mcp/ ─┐
                ├─► core/ (service, RBAC, scheduler, memory, SDI) ─► PG+pgvector (part. per org, RLS)
Browser → web/ ─┤          ▲                                    ▲
      REST/WS   │          │                                    │
              api/ ────────┘                                    │
                │                                               │
            worker/ ── IMAP · scheduler · memory/re-embed · recurrences · reminders
                │
   sdi-inbound/ (SOAP mutual-TLS, push SdI notifications)
   connectors: Gmail · Proton Bridge · SdiChannel · Conservation · Embedder
```

## Architectural principles

- `api/` and `mcp/` contain no business logic: they are two thin
  adapters over the same `core/`. GUI and MCP stay genuinely co-equal
  and do not diverge.
- Enforcement of RBAC, the state machine, (org, project) isolation and
  optimistic concurrency is in the service layer: it is the single
  choke point crossed by GUI, REST and MCP.
- Pluggable abstractions (`SdiChannel`, `ConservationProvider`,
  `Embedder`, `LLMProvider`) with a DB-driven factory and neutral DTOs,
  reusing the bitvision_phoenix pattern (see
  [ADR-0012](adr/0012-llm-embedder-abstraction.md) and
  [references.md](references.md)).
- v1 deploy: Docker Compose on a cloud ARM node; K8s-ready design.

## Monorepo layout (indicative)

```
flow/
  core/        # domain + service layer (Python package)
  api/         # FastAPI REST + WebSocket
  mcp/         # MCP server
  sdi-inbound/ # inbound SdI SOAP service
  worker/      # background jobs
  web/         # React/TS SPA
  deploy/      # Docker Compose, config, migrations
  docs/        # this documentation
```
