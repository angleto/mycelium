# web/ — Mycelium SPA (React/TS)

Browser client for Mycelium. Co-equal to the MCP layer: both are thin
clients over the same `core/` service layer through the REST API
(see `docs/architecture.md`). No business logic here.

## Contract

Types are **generated from the backend OpenAPI**, never hand-written:

```sh
pnpm gen:api   # backend must be running on :8000
```

This rewrites `src/shared/schema.d.ts`. The typed client
(`openapi-fetch`) and all calls are bound to that schema, so a backend
contract change surfaces as a type error here.

## Develop

The backend (FastAPI on :8000) must be up. The browser talks to
`/api`, which Vite proxies to the backend (a reverse proxy does the
same in the Docker Compose deploy):

```sh
pnpm install
pnpm dev        # http://localhost:5173
```

Auth: signup returns a JWT + org id, kept in a client session. The
token is sent as the `Authorization` bearer; `X-Org-Id` is per-request
tenant scoping carried explicitly on typed calls.

## Gates

```sh
pnpm lint
pnpm check:shared   # src/shared imports nothing outside itself
pnpm check:i18n     # every static t('...') resolves
pnpm check:css      # a de-filled button sets its own ink
pnpm typecheck      # tsc -b -- NOT `tsc --noEmit` from the repo root
pnpm test
pnpm build          # tsc -b && vite build
```

`make web-check` from the repo root runs the same sequence CI does.

## `src/shared`

Rules that belong to the REST API rather than to any one client: how the
error envelope reads, what an entity code is, what a recents row is, what
the search-click payload contains, what the query grammar's tokens mean,
and the generated `schema.d.ts`. This directory is compiled into the
browser extension as well as the SPA, so **it must import nothing outside
itself** -- no React, no i18next, no store, no transport. Everything is
taken as an argument and a value is returned; the caller owns the
collaborator. `src/shared/index.ts` is the only entry point: deep imports
are refused. `pnpm check:shared` enforces both rules, because neither can
surface as a type error on this side of the boundary.

## Status

Built in phases mirroring the backend roadmap (W0 scaffold + auth,
then W1..W8 per domain). UI strings go through the i18n catalog
(`src/i18n`, English default); no hardcoded strings.
