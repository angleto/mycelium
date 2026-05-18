# ADR-0015 RLS: two Postgres roles and SECURITY DEFINER provisioning

Status: accepted. Emerged while implementing F0.

## Context

ADR-0002/0007 mandate RLS as the primary defense. Two PostgreSQL facts
make it non-trivial:

1. A **superuser always bypasses RLS** (even with FORCE). If the app
   connects as superuser (the `postgres` image default), RLS isolation
   is a no-op.
2. RLS + `FORCE` makes creating a new organization a chicken-and-egg
   problem: the INSERT into `organizations` cannot satisfy a policy
   requiring `app.current_org` for an org that does not exist yet.

## Decision

- **Two roles**: `flow` (owner/superuser: DDL, migrations) and
  **`flow_app`** (runtime: LOGIN, NOSUPERUSER, non-owner, subject to
  RLS+FORCE). The app connects as `flow_app`; migrations as `flow`.
- **RLS + FORCE** on every org-scoped entity; policy on
  `nullif(current_setting('app.current_*', true),'')::uuid`
  (fail-closed: GUC absent -> no rows).
- **Tenant provisioning** via a `SECURITY DEFINER` function
  `provision_organization(name, user_id)` owned by `flow` (runs as
  owner, so it can create org+membership), with a fixed `search_path`,
  `EXECUTE` granted only to `flow_app`. The single point that creates an
  org; no RLS bypass scattered through the code.
- The role is created (without a password) by the baseline migration
  for schema idempotency; the `flow_app` password is set by a separate
  bootstrap (`deploy/bootstrap_roles.sql`) from an environment
  variable: no secret in git.

## Consequences

- RLS isolation is actually enforced (the app is not superuser): the F0
  verification tests connect as `flow_app`.
- Operational order: bootstrap role+password, then `alembic upgrade`.
- `memory_blobs` is `PARTITION BY HASH (org_id)` (composite PK
  `(id, org_id)`, a partitioning constraint); RLS on the parent table
  applies to the partitions.
- `activity_log` is append-only via a trigger that forbids
  UPDATE/DELETE.

## Alternatives rejected

- App as superuser/owner: RLS would not be enforced (bypass).
- Ad-hoc RLS bypass in code for provisioning: scatters the privilege;
  the SECURITY DEFINER function confines it to one point.
- Role password in the migration: a secret in version control.
