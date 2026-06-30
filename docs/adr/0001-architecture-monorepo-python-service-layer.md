# ADR-0001 Architecture: Python monorepo, single service layer

Status: accepted.

## Context

We need a co-equal GUI and MCP (same logic, two clients) plus a worker
and an inbound SdI SOAP service. The risk is duplicating the domain
logic between REST and MCP and letting them diverge.

## Decision

Python monorepo. `core/` holds the domain + service layer with ALL the
business logic, RBAC enforcement, the state machine, the scheduler, the
memory engine, and SDI XML generation/validation. `api/` (FastAPI REST
+ WebSocket) and `mcp/` (Python SDK) are thin adapters over the same
service layer. React/TS frontend with types generated from OpenAPI.
`worker/` for jobs, `sdi-inbound/` for push SdI notifications.

## Consequences

- A single choke point for RBAC, isolation, optimistic concurrency and
  the state machine: GUI, REST and MCP cannot diverge.
- Every domain phase exposes REST + MCP tools from the start.
- Required discipline: no logic in `api/` or `mcp/`.

## Alternatives rejected

- Separate services for REST and MCP: logic duplication and drift,
  exactly the risk to avoid.
- Non-Python backend: the reference MCP SDK is Python and the reused
  LLM/Embedding abstraction pattern (ADR-0012) is Python.
