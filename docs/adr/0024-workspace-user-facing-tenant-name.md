# ADR-0024 "Workspace": user-facing name of the tenant; personal-first

Status: accepted. Corrects a UX/product mistake caught in review:
forcing the user to name an "organization" at signup, and treating the
tenant as a login-time choice, is wrong for a personal-first assistant.
Refines, does not regress, ADR-0015 (RLS provisioning) and ADR-0002
(isolation/concurrency).

## Context

Flow is a personal assistant first, multi-tenant second. A person may
belong to several tenants but must never choose one at login, and must
never log out to switch. The early scaffold asked for an "Organization
name" at signup and bound the session to one org, which the user
rejected.

## Decision

- The user-facing concept is **Workspace**. Internally the tenant stays
  `org`/`org_id`: RLS, the `organizations`/`memberships` tables and the
  `provision_organization` SECURITY DEFINER boundary (ADR-0015) are
  **unchanged**. The rename lives only in the adapters (API/MCP
  schemas, the `X-Workspace-Id` header, routers, the i18n catalog
  text) and in user-facing docs, consistent with architecture.md
  (api/mcp are thin adapters; core is the single source of truth).
- **Personal-first signup**: `/auth/signup` never requires a workspace
  name. A personal workspace is auto-provisioned; naming it (or a
  display name) is optional.
- **In-app switching, no re-auth**: the JWT is the user identity; the
  active workspace is per-request (`X-Workspace-Id`). `GET/POST
  /workspaces` are authenticated by the user with no tenant context
  (pre-selection), backed by the SECURITY DEFINER
  `list_user_organizations` function (migration 0014), so the
  switcher works in one session.
- The stable machine error codes keep their legacy `org.*` string
  values (e.g. `org.not_found`); only the rendered text is "workspace".
  Codes are internal identifiers, not user-facing copy.

## Consequences

- Zero regression on the green backend: core/domain and the DB schema
  are untouched; the change is localized to the adapter layer + i18n
  text + this ADR + primary-doc terminology.
- Historical ADRs keep their original "organization" prose; this ADR is
  the authoritative terminology going forward.
- Follow-up (tracked, not a band-aid): the MCP tool parameter is still
  named `org_id`. Renaming it to `workspace_id` across ~50 co-equal
  tools is mechanical but is sequenced into its own dedicated pass to
  avoid a risky big-bang refactor during the auth port (W1b). MCP
  capability stays genuinely co-equal; only the parameter label lags.

## Alternatives rejected

- Renaming the internal tenant (tables/columns/RLS) to "workspace":
  large, risky, no functional gain; ADR-0015 explicitly stays.
- Keeping "Organization" user-facing: rejected by the user as wrong for
  a personal-first assistant.
- Workspace selection at login / re-auth to switch: defeats the
  personal-assistant UX; the backend already supports per-request
  tenant scoping.
