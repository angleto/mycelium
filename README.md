<p align="center">
  <img src="assets/flow-logo.svg" width="96" height="96" alt="Flow logo" />
</p>

<h1 align="center">Flow</h1>

<p align="center">
  A multi-tenant personal work hub: tasks, time &amp; billing, notes,
  clients/projects, workflows, invoicing — with an MCP control surface
  co-equal to the GUI.
</p>

---

Flow unifies a task manager, a time tracker, a scheduler, notes with a
hierarchical memory and semantic retrieval, clients/projects,
configurable workflows, and Italian electronic invoicing (SDI). Every
surface is also exposed over **MCP**, so an AI agent can drive the whole
system exactly like the web UI.

## Highlights

- **Workspaces with real RBAC.** Each workspace is an isolated tenant
  (PostgreSQL row-level security). Roles are sudo-style: you act as a
  normal *user* by default and explicitly switch up to *owner* (or, for
  the platform operator, *admin*) only when you need to. A member added
  later can never eject the workspace owner; no cross-workspace leakage.
- **Notes → tasks.** Every note belongs to a client (default
  "Personal"); convert a note to a task or spin a task off a text
  selection, inheriting tags.
- **Time &amp; billing on the client.** Hourly rate and billable default
  live on the client; every task resolves to a project and a client.
- **Clients &amp; projects, workflows, invoicing**, tag scoping, a
  dependency graph, scheduler, email-to-task, notifications.
- **MCP server** (100+ tools) mirroring the API for agent control.
- **Tested.** Backend pytest + a Playwright end-to-end suite.

## Stack

Python (FastAPI, SQLAlchemy async, Alembic) · PostgreSQL + pgvector ·
Redis · React + TypeScript + Vite · uv workspace · MCP.

## Quick start (local dev)

Prereqs: Docker, [uv](https://docs.astral.sh/uv/), Node + pnpm.

```
# 1. infra (Postgres + Redis)
make up
FLOW_DB_APP_PASSWORD=flow_app make db-bootstrap
make migrate

# 2. an admin account
FLOW_ADMIN_EMAIL=you@example.com FLOW_ADMIN_PASSWORD='a-strong-pass' \
  uv run python -m flow_core.bootstrap_admin

# 3. backend (API)
FLOW_JWT_SECRET=... FLOW_SECRET_KEY=... make run-api

# 4. frontend
cd web && pnpm install && pnpm dev
```

Backend tests: `make test`. End-to-end (servers up + a seeded
account): `cd web && pnpm e2e`.

Architecture, domain model, decisions and ADRs live in
[`docs/`](docs/README.md).

## License

Flow is free software licensed under the **GNU Affero General Public
License v3.0 or later** (`AGPL-3.0-or-later`). You may use, study,
share and modify it; if you run a modified version to provide a network
service, you must make the complete corresponding source of your
version available to its users. See [`LICENSE`](LICENSE).

SPDX-License-Identifier: AGPL-3.0-or-later
