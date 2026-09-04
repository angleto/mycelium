# ADR-0056: An indexing class on the row, not a read boundary

Status: Accepted (2026-09-04)
Relates to: ADR-0005 (hierarchical memory on pgvector, hybrid RRF
retrieval), ADR-0007 (hard memory isolation per (org, project)),
ADR-0002 (tenant invariants in the schema, RLS as the primary defense),
ADR-0049 (working memory is delegated to the calling agent), migration
`0007` (`index_scope` on `tasks` and `notes`).

## Context

Two indexers write to `memory_blobs` on their own initiative. A task's
title, description and checklist are rendered and indexed on every
mutation (`services/task_search`), and the blob is written with
`project_id=None` -- deliberately, so a task hit is org-wide. A note is
indexed one blob per part (`services/note_search`), project-scoped.
Neither asked anyone: indexing is a consequence of the row existing.

There was no way to say no. `index_scope` did not exist in any form,
there was no argument on any surface, and the only way to keep text out
of the index was to not write the row, or to delete it. The blob is not
a copy anyone maintains either: deleting it by hand is undone by the
first mutation of the source row, and by the pointer backfill sweep
within 60 seconds regardless.

So anything typed into a task description became, without further
choice, retrievable org-wide through `memory_search` and the unified
`/search` -- both of which answer to `org_id` alone. For a workspace
where an agent bootstraps from recall, that is a store-to-context path
nobody opted into.

## Decision

A column `index_scope` (`org | none`, `server_default 'org'`, no
backfill) on `tasks` and on `notes`, consulted by both indexers.

- **The guard runs before the `content_hash` short-circuit**, not before
  the blob write. A scope flip does not change the rendered text, so the
  hash is identical and a guard placed lower is unreachable on any row
  that is already indexed -- which is exactly the case that needs the
  remedy.
- **On `none` the guard deletes and returns.** Skipping would leave the
  existing blob indexed for good.
- **The task branch deletes through the pointer**, which is `PK(task_id)`
  and names the one blob a task has. **The note branch deletes by
  provenance** (`erase_blobs_for_sources`), because a part will own N
  blobs as soon as long parts are chunked and the pointer would name one
  of them.
- **The scope lives on `notes`, while the indexed unit is `note_part`.**
  No mapper listener is registered on `Note`, so the flip cannot rely on
  one: `update_note` fans it out over the note's parts, dropping their
  blobs on `none` and marking them dirty on `org`. Both directions are
  idempotent, so no caller has to read the previous value.
- **Both pointer-backfill sweeps exclude scoped-out rows in SQL.** Such
  a row has no pointer by definition, so it would otherwise be a
  permanent candidate, filling an unordered `LIMIT` batch with rows the
  resync discards and starving the real backlog.
- **A revision records the value and a restore never writes it back.**
  Reverting a task must not silently put back into the index a row
  somebody took out of it.
- **Index maintenance runs without the caller's project perimeter.**
  `p_memory_blobs` carries a project term that `p_blob_sources` and the
  source tables do not, so a request that arms `app.current_project` can
  write the row while the blob derived from it is invisible. That made the
  opt-out silently conditional on a request header: on the task side the
  blob DELETE matched nothing and the flip looked applied, and on the note
  side the provenance DELETE succeeded while the blob DELETE did not,
  leaving the note's text in a blob no erase-by-provenance path could ever
  reach again. The flush now neutralises that GUC for its window; the org
  term still binds, and it is the only one that ever governed these rows.
- **The transplant carries the class into the task it becomes, in either
  order.** `promote_note_to_task` copies the note's own body into the task's
  description, so a task born at the default would put a scoped-out note's
  text straight back into the org-wide index; and a note scoped out AFTER
  the promotion carries the class onto that task, because the copy is
  already there and a task blob is written `project_id=NULL`, a wider
  perimeter than the note's own part blobs. One direction only: pushing
  `org` back onto the task would re-index a task its owner may have scoped
  out on its own, and the promotion froze the note while the task's
  description went on living.
- **A transplanted note can still be scoped out.** ADR-0029 D2 makes a
  promoted note read-only, and that guard states its own reach as "every
  CONTENT mutation". The indexing class is not content, and fencing it
  there would have left a whole class of rows with no way out of the index
  while this ADR claims the flip is the remedy.
- **A stated `null` is refused, not written.** Both PATCH surfaces type the
  field as optional, so `{"index_scope": null}` is well-formed and can only
  end as a NOT NULL violation. It is refused once, where versioned writes
  funnel, with the field named -- which also closes the same latent 500 on
  every other NOT NULL column.

## Consequences

`index_scope='none'` is an opt-out from *automatic indexing*, and the
limit is the point of the decision rather than an oversight:

- the row stays readable org-wide. `get_task` selects on the primary key
  with no actor predicate, and the RLS policies carry `org_id` as their
  only term;
- the row stays matchable by the server-side free-text filter of
  `list_tasks(q=...)` and `list_notes(q=...)`, which ILIKE the live
  columns and never touch `memory_blobs`;
- revision snapshots taken before a flip keep the text that was there,
  readable until the row is purged;
- a blob that carries a second provenance survives the erase by design,
  because it is no longer only this row's. Consolidation is the case that
  exists: a concept blob inherits each member's provenance, so the erase
  reaches it and takes this row's provenance row off it, and the concept
  itself survives on the other members' rows, still carrying the merged
  text;
- a blob written on purpose is not touched. `memory_write`, and the admin
  re-index of legacy whole-note sources, carry their own provenance
  (`source_kind='note'`) rather than the automatic per-part kind; they are
  deliberate index writes, which is the thing this column does not govern;
- derivations other than the transplant do not propagate the class. A
  distilled note, a task derived from a note through the garden
  suggestions: each is a new row that takes the default. Propagating the
  class through every derivation is a policy question this ADR does not
  settle, and the transplant is carved out only because it copies the body
  itself with no caller in between.

What it closes is unrequested recall. What it does not close is a
deliberate query by an in-org actor who can already read the row.
**Material that must not be readable by everyone in the org still does
not belong in a title or a description, at any scope.**

There is no backfill: the server default preserves the behaviour of
every existing row, and nothing sweeps retroactively. The remedy for
text already indexed is the explicit flip, row by row, which deletes
that row's blobs.

## Alternatives rejected

- **The column on `note_part` instead of `notes`.** A part added to a
  scoped-out note would be born at the `'org'` server default and
  silently re-index the note. Scope is a property of the document.
- **Making it a read boundary.** A per-actor read predicate, or a
  separate store, is a different and much larger decision; shipping a
  half of it under a name that sounds like the whole would be worse than
  not shipping it. The name says `index_scope` for that reason.
- **Extending it to the free-text `q=` filter.** Deferred deliberately.
  The distinction that makes the deferral defensible is that a caller
  using `q=` is asking while the indexer is telling, and that a caller
  who can `get_task` the row already has its text.
- **A guard immediately before the blob UPSERT.** Correct-looking and a
  silent no-op on every already-indexed row.
- **Deleting the row instead.** Stronger, and still the right answer when
  the text must be gone rather than unindexed: emptying the trash erases
  blobs by provenance before the row delete, and the revision cascade
  takes the snapshots with it. `index_scope` is for rows that must keep
  existing.
