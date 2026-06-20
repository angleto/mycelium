# ADR-0042 — Tasks as first-class graph nodes + complete auto-classify-on-ingest

Status: Proposed (2026-06-20) — supersedes the notes-only scope of ADR-0032
P4 (auto-classify) for the task dimension. Task: `b8c60940` (WS-D2,
"versione completa" chosen by Angelo 2026-06-20).

## Context

`garden_classify` (ADR-0032) proposes a structured classification —
`{cluster, tags, links, maturity}` — for a node, as read-only proposals
the forester accepts/overrides (never auto-applied). Today it has two
limits, both of which the chosen scope removes:

1. **Notes only.** `classify_node` resolves `select(Note)` and raises
   `NOTE_NOT_FOUND` for anything else (`garden_classify.py:393-398`).
   Tasks are never classified.
2. **Sweep only, cluster-only marker.** The autonomous path
   (`autoclassify_unprocessed`) runs in the periodic garden sweep and
   stamps notes with a structural `auto_cluster` marker; it does not run
   at creation and does not pre-compute tag/link suggestions.

The unified graph that powers cluster/centrality/link-prediction
(`graph.py`) is **notes-only**: its node set is `note_rows`, its edges
are `note_note_link` + the note↔tag bipartite + co-activity. Tasks carry
the signals needed to join that graph — `TaskRelation` (symmetric
task↔task "related" edges, already a table), `NoteTaskLink` (typed
note↔task links), task tags (`TaskTag`), activity-log entries
(co-activity), and `memory_blob` embeddings via the task index pointer —
but they are not currently nodes in it.

"Complete on tasks" (the chosen option B) therefore requires tasks to
become **first-class graph nodes**, not a small wiring change. This ADR
records that subsystem and the complete auto-classify built on top.

## Decision

### D1. The unified graph includes tasks

Extend the graph node set from notes to **notes ∪ tasks**. Edges:

- note↔note: `note_note_link` (unchanged).
- task↔task: `TaskRelation` (`related`, undirected), tag-induced
  (Adamic-Adar over `TaskTag`), co-activity.
- note↔task: `NoteTaskLink` (the four typed kinds), with a per-kind
  weight in the soft-OR (subject/artifact/derived_from/promoted_from).
- node↔tag bipartite spans both `NoteTag` and `TaskTag`.

`compute_note_edge_weights`, PageRank, PPR, betweenness, and Leiden all
operate over the combined node set. The graph snapshot signature folds
in the task edges so it recomputes when they change. **Blast radius:**
this changes what the existing mindmap, `/garden/clusters`, centrality
(`d8664631`), and the closed WS-* graph tasks render — so it ships
behind a flag (`garden_unified_task_graph_enabled`, default false) and
the snapshot keeps a notes-only mode until the flag flips, with a
regression guard that the notes-only signature is byte-identical when
the flag is off.

### D2. `classify_node` accepts tasks

Resolve note-or-task by id (try `Note`, then `Task`); set
`node_kind ∈ {note, task}`. Suggestions per kind:

- **tags** — both: co-occurrence over `NoteTag ∪ TaskTag` in the node's
  graph neighbourhood. Cold-start damping (`990f0fa2`) uses the combined
  node count.
- **cluster** — both, once D1 lands (tasks are in the Leiden graph).
- **links** — both: notes suggest note-links; tasks suggest `related`
  task-links (link-prediction over the task subgraph).
- **maturity** — **notes only.** See D3.

`apply_suggestion` gains a task branch (e.g. `attach_tag` →
`tasks.attach_tag`); the bus mapping + is_inert gate are unaffected
(tasks are not subject to the note inertness invariant; an agent
applying a task tag follows the existing task RBAC).

### D3. "Maturity" is not a task primitive — RESOLVED: maturity = N/A for tasks (Angelo, 2026-06-20)

Note maturity (`seed/growing/mature/dormant`) is the foresta freshness
lifecycle. **Tasks have workflow states** (`todo/in_progress/verify/
done/…`), a different and authoritative lifecycle. Suggesting a "maturity"
for a task would invent a parallel lifecycle that conflicts with its
workflow state.

**Decision (confirmed by Angelo 2026-06-20):** classify_node returns
`maturity=None` for tasks — it is N/A; the task's workflow state is its
lifecycle. "Complete on tasks" = **cluster + tags + links** for tasks. A
task lifecycle *suggestion* (e.g. "this task looks ready for verify"), if
ever wanted, is a separate **workflow-state suggestion** feature, not
maturity.

Note on the corpus flag: the whole task-unification (classify accepting
tasks, and the tag co-occurrence / graph spanning notes∪tasks) ships
behind `garden_unified_task_graph_enabled` (default false). With the flag
OFF, classify_node(task) raises NOT_FOUND exactly as today and the note
path is byte-identical (notes-only corpus); with it ON, tasks are
classifiable and the corpus unifies. So no default behaviour change and
no regression to the closed note-classify tests.

### D4. Pre-computed suggestions are persisted (TTL)

New table `precomputed_suggestion` (org-scoped, FORCE RLS):
`(org_id, node_kind, node_id, suggestion_type, suggestion_value JSONB,
confidence, rationale, computed_at)`, PK/unique on
`(org_id, node_id, suggestion_type, …)`. The classification job writes
the `classify_node` output here. The classify read endpoint serves the
persisted suggestions when fresh (`computed_at > now - TTL`, default 1h),
else recomputes live; a `source` field (`precomputed`/`live`) and a
refresh control surface the freshness in the SPA. Staleness is bounded by
the TTL; the graph changing within the window is acceptable (these are
proposals, the human decides).

### D5. Classify on creation (queue)

New `classification_job` queue row `(org_id, node_kind, node_id, status,
created_at, processed_at)`. `create_note` / `create_task` enqueue a job
after the audit write, in the same transaction (so a rolled-back create
enqueues nothing). A worker step (`process_classification_jobs`, gated by
`garden_autoclassify_on_creation_enabled`) drains the queue, runs
`classify_node`, and writes `precomputed_suggestion` (D4). Non-blocking
to create; backpressure via a per-tick cap (like the email ingest cap).
A one-time backfill enqueues existing un-classified nodes.

### D6. SPA

`GardenSuggestionsPanel` reads the persisted suggestions (D4) for notes
**and** tasks, shows the freshness/source + a refresh, and keeps the
accept/dismiss = proposal-not-imposition contract. Tasks gain the panel
on the task detail view.

## Invariants preserved

Proposal-not-imposition (nothing auto-applied), transparency (confidence
+ rationale + `source`), reversibility (suggestions are advisory rows;
applying routes through the audited `apply_suggestion`). The note
anti-mutation invariant (§12 / `8a26c000`) is untouched — classification
writes proposals, never mutates a live node's body.

## Sequencing (each step ships behind its flag, verified, before the next)

1. **D4 store** — `precomputed_suggestion` table + migration + read/write
   service + tests. (No behaviour change yet.)
2. **D2-tags** — `classify_node` accepts tasks + tag suggestions for tasks
   (no graph change needed; co-occurrence only) + `apply_suggestion` task
   branch + tests. Feasible today, lowest risk.
3. **D5 queue** — `classification_job` + enqueue on create + worker drain
   (flag) writing to D4 + backfill + tests.
4. **D1 graph** — tasks as graph nodes (flag) + snapshot signature +
   regression guard (notes-only byte-identical when off) + tests. Highest
   blast radius; isolated step.
5. **D2-cluster/links** — task cluster + task link-prediction on top of D1
   + tests.
6. **D6 SPA** — persisted-suggestion panel for notes + tasks, freshness +
   refresh; i18n en/it; e2e.

## Alternatives considered

- **Option A (notes complete, tasks tags-only)** — rejected by Angelo in
  favour of B; it avoids D1 entirely (no task graph) and is a fraction of
  the work, but never gives tasks cluster/link suggestions.
- **No persistence (marker + on-demand)** — rejected by Angelo in favour
  of D4; simpler (no table) but suggestions aren't ready at first open.
