<p align="center">
  <img src="assets/flow-logo.svg" width="96" height="96" alt="Flow logo" />
</p>

<h1 align="center">Flow</h1>

<p align="center">
  A multi-tenant personal work hub: tasks, time &amp; billing, notes,
  clients/projects, workflows, invoicing — with an MCP control surface
  co-equal to the GUI.
</p>

<p align="center">
  <a href="https://github.com/angleto/flow/actions/workflows/ci.yml"><img src="https://github.com/angleto/flow/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL%20v3%20or%20later-blue.svg" alt="License: AGPL-3.0-or-later" /></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-blue.svg" alt="Python 3.12+" />
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff" /></a>
  <a href="https://mypy-lang.org/"><img src="https://www.mypy-lang.org/static/mypy_badge.svg" alt="Checked with mypy" /></a>
  <img src="https://img.shields.io/badge/web-React%2019%20%2B%20TypeScript-61dafb.svg" alt="React 19 + TypeScript" />
  <img src="https://img.shields.io/badge/MCP-control%20surface-8A2BE2.svg" alt="MCP control surface" />
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
- **Keyboard-first CLI + Neovim plugin.** `brew install
  angleto/tap/flow-cli` for the terminal client (`flow today`, `flow
  task add`, `flow timer start`, `flow note voice`, …) and
  [`nvim/flow.nvim`](nvim/flow.nvim/README.md) for the in-editor
  surface. Both shell out to the same REST API; see
  [`docs/cli.md`](docs/cli.md).
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

## Trademark

"Flow" and the Flow logo are trademarks of Angelo Leto. The AGPL grant
above covers the source code; it does not grant permission to use the
project name or logo. Forks and derivative distributions must be
released under a different name and a different mark. You may describe
your project as "based on Flow" or "compatible with Flow", but you may
not redistribute it under the "Flow" name, nor use the logo, in a way
that would suggest endorsement by or affiliation with the upstream
project.

Contributions to the upstream project must be signed off under the
[Developer Certificate of Origin](CONTRIBUTING.md#developer-certificate-of-origin-dco).
