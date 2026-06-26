# web/ — Mycelium SPA (React/TS)

Browser client for Mycelium. Co-equal to the MCP layer: both are thin
clients over the same `core/` service layer through the REST API
(see `docs/architecture.md`). No business logic here.

## Contract

Types are **generated from the backend OpenAPI**, never hand-written:

```sh
pnpm gen:api   # backend must be running on :8000
```

This rewrites `src/api/schema.d.ts`. The typed client
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
pnpm build      # tsc -b && vite build
```

## Status

Built in phases mirroring the backend roadmap (W0 scaffold + auth,
then W1..W8 per domain). UI strings go through the i18n catalog
(`src/i18n`, English default); no hardcoded strings.
