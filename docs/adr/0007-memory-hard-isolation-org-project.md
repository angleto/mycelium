# ADR-0007 Hard memory isolation per (org, project)

Status: accepted. Explicit user requirement.

## Context

The system will manage a lot of information organized on the user's
behalf. Requirement: do not mix one project's memory with another's, no
data leak, neither across tenants nor across projects. A relevance-only
filter is not an isolation guarantee (see the filtered-ANN trap: a
selective `WHERE` over HNSW can degrade to an unreliable post-filter).

## Decision

A **hard** boundary, not by relevance. `memory_blobs` partitioned by
`org_id`, HNSW/GIN/trigram indexes per partition. The
(org_id, project_tag_id) predicate is **mandatory** in every memory
query. Mandatory RLS. Default = current project; cross-project access
only with explicit, audited authorization. No
retrieval/summarization/consolidation crosses projects or tenants;
consolidation is limited to the same (org, project, thread/account) and
never cross-subject. The rule holds identically via MCP (FR-10).

## Consequences

- Isolation and data security guaranteed by RLS + partition +
  predicate; the metadata pre-filter remains relevance only.
- Mandatory test: an unfiltered search never returns data from another
  project/tenant.

## Alternatives rejected

- Org-level isolation only with project as relevance: violates the
  requirement (would mix projects).
- Relying on the application-level query alone: a forgotten predicate =
  leak; defense in depth is needed (RLS + partition).
