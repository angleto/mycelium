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
  bootstrap (`deploy/local/bootstrap_roles.sql`) from an environment
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

## Amendment (2026-08-22): the owner is NOT a superuser in production

Fact 1 above ("a superuser always bypasses RLS") is load-bearing: it is
what lets a migration read and write across tenants. On **managed**
PostgreSQL it does not hold, because the provider does not hand out
superuser:

    dev / CI (postgres image)   mycelium: rolsuper=t  rolbypassrls=t
    production (Scaleway)       mycelium: rolsuper=f  rolbypassrls=f

So in production the migration role is subject to `FORCE`, the policies
are fail-closed on an unset `app.current_org`, and **every backfill on an
org-scoped table updates zero rows and raises nothing**. The defect is
invisible in dev and CI — where the role *is* a superuser and the tests
pass — and only manifests in production.

This was first hit at 0011 — which shipped the bug and cost a data
recovery in 0013 — then again at 0035/0036, worked around inside 0037
alone (see its docstring). The runner was never fixed, so the bracket had
to be remembered on every later revision, and five forgot it: 0016, 0022,
0039, 0095, 0099.

Audited in production on 2026-08-22: 0099 and 0039 had really no-opped
and were repaired (22 annotations re-anchored to the source domain, 48
time entries realigned); 0022 and 0095 had no rows to touch; 0016 is not
retroactively verifiable (it dropped the source column) but its residue
is legal under the asymmetric note invariant. Per-migration outcomes are
in `core/migrations/archive/README.md`.

The runner now closes it centrally (`mycelium_core.migration_rls`): when
the role does not already bypass RLS, `FORCE` is lifted for the duration
of the migration transaction and restored on the way out. `FORCE` exists
to constrain the OWNER — a non-owner is subject to the policies with or
without it — so this restores exactly the semantics this ADR assumed,
leaves `mycelium_app` isolation untouched, and needs no superuser. Where
the role already bypasses RLS (dev, CI) it does nothing at all.

Granting the migration role `BYPASSRLS` would be the smaller change and
is preferable where the platform allows it; Scaleway does not, since
setting that attribute itself requires superuser.

## Alternatives rejected

- App as superuser/owner: RLS would not be enforced (bypass).
- Ad-hoc RLS bypass in code for provisioning: scatters the privilege;
  the SECURITY DEFINER function confines it to one point.
- Role password in the migration: a secret in version control.
