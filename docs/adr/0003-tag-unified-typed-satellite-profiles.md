# ADR-0003 Unified tag with typed satellite profiles

Status: accepted.

## Context

User requirement: client and project are **special tags**, a single
tag concept, no extra entity. But a client's data is legal/fiscal
(VAT number, recipient code, regime) and a project's is billing (rate,
currency, budget): putting them in free JSONB on a generic tag table
sacrifices the constraints, types and validation that invoicing
requires by law and that per-project memory isolation requires for
integrity.

## Decision

A single `tags(kind in {generic, client, project})` concept (honors
the requirement). Structured data lives in **typed satellite
profiles** with an FK to `tags.id`: `client_profile(tag_id PK, ...)`
and `project_profile(tag_id PK, client_tag_id FK, ...)`. Associating a
client/project with a task = attaching the tag (one relation per kind).

## Consequences

- A single conceptual model as required, but with referential
  integrity and validation on the fiscal and billing data.
- Reporting and invoicing queries aggregate via the client/project tags
  with joins to the profiles.

## Alternatives rejected

- Free JSONB attributes on the generic tag: no constraints, fragile
  validation on legally sensitive data.
- Client/Project entities separate from tags: violates the user's
  explicit requirement.
