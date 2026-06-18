# ADR-0041: Autonomous retention spares the originals

Status: Accepted
Date: 2026-06-18
Relates to: ADR-0016 (tier = latency, not retention), ADR-0034 (humus
policy in the walk), ADR-0039 (fungal decomposition pipeline), the
§12 anti-mutation invariant (note `90e4db3e`, task `8a26c000`).
Implemented in `services.entity_revisions.hard_delete_soft_deleted`
(driven by the `revisions_retention` worker). Task: WS-F1 `8e47c37f`.

## Context

The `revisions_retention` worker runs an autonomous sweep that
hard-deletes task and note rows whose `deleted_at` is older than
`revisions_hard_delete_after_days`; an AFTER DELETE cascade trigger then
purges their recovery history (`entity_revision`).

For tasks this is unremarkable garbage collection. For **notes** it
collides with two load-bearing promises of the forest-of-memory:

- **"Save the originals, always."** Archiving is *transformation*, not
  loss (ADR-0034/0039): a note can be decomposed into a `hypha_of`
  distillation that the LLM walk surfaces as humus. The distillation is
  a lossy compression; the rich source it grew from is the original. If
  the autonomous timer hard-deletes a soft-deleted source, the lineage
  is broken and the original is gone, silently.
- **§12 "the system is never destructive on its own."** Autonomous
  lifecycle operations act only on inert matter and are reversible and
  tracked; a permanent, untracked hard-delete by a timer is exactly the
  destructive autonomous act §12 forbids.

The tension was being resolved silently, in the timer's favour.

## Decision

The autonomous retention sweep **never hard-deletes an original**. A
soft-deleted note is spared (kept soft-deleted indefinitely, still
recoverable) when either holds:

1. `humus_flag = true` — it is humus the ADR-0034 walk can surface
   (a flagged source, or a distillation atom); or
2. it is the **source** of a distillation — a `hypha_of` *parent*, so it
   has derived nodes hanging off it.

Everything else (ordinary soft-deleted notes, all soft-deleted tasks)
is hard-deleted past the cutoff as before.

This is a hardcoded safety invariant, not a tunable: the safe failure
mode for "save the originals" is to under-delete. The guard lives in the
DELETE predicate (`humus_flag = false AND NOT EXISTS (hypha_of child)`),
so it is atomic with the sweep and RLS-scoped to the tenant.

### What this is NOT

It does **not** block erasure. **Explicit, user-initiated deletion stays
sovereign**: a GDPR erasure cascades by provenance (ADR-0005) and a user
hard-deleting their own note is their call. This ADR gates only the
*timer* — the one actor that has no human in the loop. The distinction
that matters is autonomous-cleanup (must not destroy originals) vs
user/legal erasure (must delete); only the former is constrained here.

## Consequences

- A decomposed-then-soft-deleted note accumulates instead of being
  purged. Acceptable: humus sources are the corpus's value, not its
  bloat, and they remain queryable/recoverable rather than tiered out.
- A genuinely unwanted note that happens to be a `hypha_of` parent will
  not be auto-purged; the user can still erase it explicitly. Surfacing
  "spared by retention because it is a humus source" in the trash UI is
  a follow-up (transparency), not part of this guard.
- Considered and rejected: *demote-not-delete the body* (conflicts with
  the timer's legitimate cleanup role and GDPR) and a *blob-level
  canonical-original facet* (more surface, pushes the call to "who pins
  what" with ambiguous defaults). The source/humus guard is the minimal
  policy that keeps the promise.
