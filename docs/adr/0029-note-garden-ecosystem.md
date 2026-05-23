# ADR-0029: Note garden ecosystem and typed note/task relations

Status: Proposed
Date: 2026-05-23
Relates to: ADR-0028 (Identity-first addressing), ADR-0027
(Adjudication framework), ADR-0025 (orchestration), ADR-0005
(hierarchical memory), ADR-0020 (voice notes), Proposal A
(`flow-mechanism-decisions`).

## Context

Flow already treats notes as first-class citizens (capture via voice
/ Telegram / SPA, embedding-backed memory, tag system, link to tasks
via Proposal A). What the model is missing, and the user has been
explicit about, is **the substrate of extended thinking**: a place
where ideas live before they become work, grow over time, sometimes
die, sometimes return.

Three observations, all from the same user discussion:

1. **Notes are how I think.** They are not documents; they are the
   medium in which ideas mature. The bias is toward elaboration:
   capture and reference are means, elaboration is the end.
2. **The flow between notes and tasks is bidirectional and
   non-deterministic.** A note can produce several tasks while
   itself staying alive. A note can become a task (transplant). A
   task can be the work of growing a note. A task can leave a note
   behind as artifact. These are four distinct semantic relations,
   not variations of one.
3. **Tasks must stay light.** The task model already carries
   workflow, scheduling, billing, accountability (ADR-0028); adding
   a "reasoning task" mode that does not respect any of those rules
   would bloat the abstraction. Reasoning belongs to notes, not to
   tasks pretending to be lighter.

Three concurrent capabilities now in the schema make a richer note
ecosystem feasible and coherent:

- **Identity polymorphism** (ADR-0028): a "gardener" is either a
  user or an ai_assistant, addressed uniformly. Agent and human
  collaborate on the same notes symmetrically.
- **Audit actor_kind** (commit `a4ede7a`): every mutation records
  who acted and how (human_direct, agent_run, mcp_token, ...).
  Notes inherit honest attribution.
- **Adjudication** (ADR-0027): multi-agent convergence framework
  available when two agents propose divergent directions for the
  same note.

The current Proposal A (`note.task_id` optional FK) carries only one
relation kind (artifact) and is unidirectional in the schema. Closing
the gap requires moving to a typed M:N link table, plus a maturity
lifecycle for notes, plus a note↔note link table for the
Zettelkasten-style structure the user already practices implicitly.

## Decision

### D1. The garden paradigm as architectural foundation

Notes are **plants in a garden**. Tasks are **fruits and
transplants**: actions that emerge from mature plants without
necessarily consuming them. The metaphor is not decorative: it maps
1:1 to schema choices, and it justifies why notes get a lifecycle
and tasks do not get a "reasoning kind".

| Garden concept | Flow entity | Meaning |
|---|---|---|
| Plant | `Note` | Grows, blooms, may wither or recover |
| Seed | `note.maturity='seed'` | Fresh capture, untouched |
| Young plant | `note.maturity='growing'` | Recently touched, you're working it |
| Mature plant | `note.maturity='mature'` | Crystallised, ready to fruit |
| Dormant plant | `note.maturity='dormant'` | Untouched for a long time |
| Fruit | Task linked via `derived_from` | An action falls out, plant stays |
| Transplant | Task linked via `promoted_from` | The plant moves to the work garden |
| Watering | Task linked via `subject` | Work whose purpose is to grow the note |
| Compost | Task linked via `artifact` | Work that leaves a new note behind |
| Path | Note linked via `references` / `atom_of` | Plants connect to each other |
| Season | maturity transition rules | Plants change state over time |
| Gardener | `Identity` (user or ai_assistant) | Symmetric; agent and human both tend |

### D2. Note.maturity lifecycle

```
note.maturity  enum  seed | growing | mature | dormant
note.promoted_at  timestamptz NULL
```

Default for a freshly created note: `seed`.

Automatic transitions (regola della stagione):

- `seed` → `growing` when the note is edited or linked at least once
  within 7 days of creation.
- `growing` → `mature` when the user sets it explicitly. No
  automatic promotion to mature: maturity is a user judgement, the
  system does not decide for them.
- `growing | mature` → `dormant` when untouched (no read, no edit,
  no link change) for 60 days. The threshold is configurable per
  workspace but defaults to 60.
- `dormant` → `growing` when touched again (read counts here too,
  not just edit).

Manual override always wins over the automatic rule and is audited
(`activity_log` entry with `action='set_maturity'`).

`promoted_at` is set when a note is transplanted (D4). The note
becomes read-only at that point; it is still visible (the
transplanted plant remains in the old spot) but the new growth is
in the task.

### D3. Typed note↔note links

A new table `note_note_link` materialises the structural relations
between notes, with M:N cardinality:

```
note_note_link(
  id              uuid pk,
  org_id          uuid not null,        -- RLS tenant
  parent_note_id  uuid not null fk notes(id) on delete cascade,
  child_note_id   uuid not null fk notes(id) on delete cascade,
  kind            text not null check (kind in (
                    'atom_of', 'references', 'replies_to', 'supersedes'
                  )),
  created_at      timestamptz not null default now(),
  created_by      uuid not null fk identities(id),
  unique (parent_note_id, child_note_id, kind),
  check (parent_note_id <> child_note_id)
)
```

Four kinds:

- **`atom_of`**: child is an atomic piece that composes an index
  parent. Implements the Zettelkasten "structure note" pattern: a
  parent note collects backlinks and synthesises; children are
  small atomic pieces of thought.
- **`references`**: child cites parent. Classic backlink. The
  retrieval surface uses this to walk the citation graph.
- **`replies_to`**: child continues / elaborates parent. Like
  threading; allows building a chain of evolving thought without
  editing the original.
- **`supersedes`**: child replaces parent. The parent stays for
  history but the canonical reading is the child. UI may collapse
  the parent under the child in the default view.

The `created_by` is an Identity, not a User: an ai_assistant can
also link notes during synthesis. The audit gets actor_kind for
free.

### D4. Typed note↔task links

A second new table `note_task_link` replaces the single
`note.task_id` FK with M:N typed relations:

```
note_task_link(
  id              uuid pk,
  org_id          uuid not null,
  note_id         uuid not null fk notes(id) on delete cascade,
  task_id         uuid not null fk tasks(id) on delete cascade,
  kind            text not null check (kind in (
                    'subject', 'artifact', 'derived_from', 'promoted_from'
                  )),
  created_at      timestamptz not null default now(),
  created_by      uuid not null fk identities(id),
  unique (note_id, task_id, kind)
)
```

Four kinds, one per quadrant of the bidirectional flow described
in the context:

- **`derived_from`**: the task came out of the note as a fruit;
  the note remains alive. A note may have several `derived_from`
  tasks accumulated over time.
- **`promoted_from`**: the note became this task (transplant);
  exactly one such link per note; the note is marked `promoted_at`
  and becomes read-only.
- **`subject`**: the task is to work on the note (watering). When
  the task closes, the note carries the result; usually the same
  link survives.
- **`artifact`**: the task produced this note. Proposal A's
  semantics, lifted into the typed M:N. The existing
  `note.task_id` column is dropped in favour of this row.

A task can simultaneously hold a `subject` link (the note it's
working on) and an `artifact` link (the note it produced when it
finished). A note can simultaneously be the `artifact` of one task
and the `subject` of another (the previous result becomes the next
input). The model is honest about that.

### D5. Lifecycle operations

Four named operations cover the four flows. Each is owner / member
gated (member is enough), idempotent within reason, and audited.

- **`derive_task_from_note(note_id, title, ...)`**: create a new
  task; write a `derived_from` link; the note stays unaffected.
  The task inherits the note's tags by default. Used when "the
  note made me realise I should do X" but the note is still alive.
- **`promote_note_to_task(note_id, title?)`**: create a new task;
  write a `promoted_from` link; set `note.promoted_at = now()`;
  mark the note read-only (service layer enforces, no schema
  flag beyond `promoted_at`). The note keeps its body, its
  backlinks, its atomic children: future read-only consultation
  is supported.
- **`start_task_on_note(note_id, task_id)`**: write a `subject`
  link from an existing task to an existing note. Used when the
  user / scheduler decides "this task is to work on that note".
- **`record_task_artifact(task_id, note_id)`**: write an
  `artifact` link from a closing task to a note (new or existing,
  typically new from the run). Already happens implicitly today
  via Proposal A; surface as an explicit operation.

### D6. Symmetric collaboration between human and agent

The garden is tended symmetrically by human users and ai_assistant
identities. Both can:

- create notes (`identity.kind` recorded as the author);
- edit notes (the `activity_log` carries `actor_kind` + the
  identity, so contribution history is reconstructible);
- link notes to other notes and to tasks;
- set maturity (manual override);
- run the four lifecycle operations.

The asymmetry is **accountability**, not action. The owner (always
a real user, ADR-0028) keeps the final word: if two agents propose
divergent edits to the same mature note, an Adjudication is opened
on the conflict (D8) and the human owner is the escalation target.

### D7. Garden UI surfaces

The SPA gains a `/garden` route with three primary tabs and an
optional plant page:

- **Inbox**: notes with `maturity='seed'`. The triage surface.
  Three actions per row: archive, set tag, mature/promote.
- **Garden**: notes with `growing | mature`. Grouped by cluster
  (tag-first, embedding-fallback). Shows fruits below each plant
  (linked tasks via `derived_from` / `promoted_from`).
- **Cemetery**: notes with `dormant` (and a sub-section for
  `promoted_at IS NOT NULL`, "transplanted"). Two actions:
  resurrect (force `growing`) or compost (soft-delete).
- **Plant page**: opens any single note. Shows the body, the
  backlinks (notes that link to it), the atomic children
  (`atom_of`), the fruits (tasks via any of the four kinds),
  and the activity log filtered to that note.

The `/tasks/{id}` page (existing) gains the inverse: the related
notes section now shows the four typed relations as labelled
chips.

### D8. Multi-agent collaboration on a note: convergence on edit conflict

When two agents (or an agent and a human) propose divergent edits
to the same mature note within a short window, the integration with
the Adjudication framework (ADR-0027) opens a `DebateStrategy` over
the proposed directions. The decision becomes the next canonical
edit; residual dissent (also recorded by Adjudication) is preserved
as a `replies_to` link from a new note that captures the rejected
direction.

This is **phase 4** below: the modelling is described here for
coherence, but the implementation comes after the base model is
validated by daily use.

## Schema (recap)

Three additive changes to existing tables, two new tables. No
breaking deletion in the first migration.

```sql
ALTER TABLE notes
  ADD COLUMN maturity text NOT NULL DEFAULT 'seed'
    CHECK (maturity IN ('seed','growing','mature','dormant')),
  ADD COLUMN promoted_at timestamptz NULL;

CREATE TABLE note_note_link (...);  -- as D3
CREATE TABLE note_task_link (...);  -- as D4

-- Migrate existing Proposal A: every Note with task_id is an
-- 'artifact' link. Then drop note.task_id in a later migration
-- (zero-downtime: first the table+writes, then the FK drop).
INSERT INTO note_task_link (org_id, note_id, task_id, kind, created_by)
  SELECT n.org_id, n.id, n.task_id, 'artifact', n.created_by_identity
  FROM notes n WHERE n.task_id IS NOT NULL;
```

RLS on the two new tables per ADR-0002 (`org_id`-scoped). Indexes
on `(parent_note_id)`, `(child_note_id)`, `(note_id, kind)`,
`(task_id, kind)`.

## Integration with existing decisions

- **ADR-0025 (orchestration)**: untouched. The scheduler and
  dispatch keep operating on tasks; notes are not in their domain.
- **ADR-0027 (adjudication)**: invoked in phase 4 for edit
  conflicts on mature notes (D8). The adjudication produces a
  synthesis note and a residual dissent note linked via `replies_to`.
- **ADR-0028 (identity)**: the `created_by` columns on the two new
  link tables and on note edits reference `identities.id`. Same
  M:N idea extends naturally.
- **ADR-0005 (memory)**: notes continue to populate the memory
  layer. The maturity lifecycle is an additional axis the retrieval
  may use (a `dormant` note ranks lower; a `mature` ranks higher),
  but the embedding pipeline is unchanged.
- **ADR-0020 (voice notes)**: untouched. A voice note enters as
  any other note; its `kind` already exists; the new `maturity`
  applies uniformly.

## Phasing

Three obligatory phases plus an optional fourth, each shippable
green independently.

- **P1**: schema (`maturity`, `note_note_link`, `note_task_link`)
  + migration of Proposal A into the new typed link + maturity
  automatic transitions in a worker tick. Service layer +
  MCP tools (`set_maturity`, `link_notes`,
  `derive_task_from_note`, `promote_note_to_task`,
  `start_task_on_note`, `record_task_artifact`). Audit on every
  operation. Tests.
- **P2**: SPA Garden (`/garden` route with Inbox / Garden /
  Cemetery tabs + plant page). Inverse view on tasks (show typed
  related notes as labelled chips). i18n.
- **P3**: drop `note.task_id` column (Proposal A FK) after a
  full release cycle confirms the typed link is the only writer.
  Cleanup phase.
- **P4** (optional, after several weeks of use): Adjudication
  integration on edit conflicts (D8). Triggered when two
  identities edit the same mature note within a configurable
  window with semantically divergent content (coherence below a
  threshold, computed on the proposed bodies). Opens a
  `DebateStrategy` adjudication and surfaces it to the note
  owner.

## Consequences

- One Note entity, multiple typed relations: complexity grows in
  the relation table, not in the entity. Coherent with the
  preference for "few entities, rich semantics".
- The four note/task relations honour the bidirectional,
  multi-cardinality flow the user described. None of them
  collapses the others.
- The maturity lifecycle gives the Inbox / Garden / Cemetery UX
  a real backing.
- The Adjudication framework finds another natural home (P4)
  beyond pure dispatch: edit conflict on a mature note is a
  decision problem with multiple agent opinions, exactly its
  shape.
- Proposal A is not erased; it's lifted into a richer surface.
- Schema cost: 2 new tables, 2 new columns, 1 column dropped at
  P3.
- Migration cost: one-shot lift of `notes.task_id` rows into
  `note_task_link(kind='artifact')` is fully scripted; no data
  loss.

## Alternatives considered

- **A. Promote-only path** (the original M2 of the previous
  iteration). Rejected: one of four flows; would lock the model
  into a single direction and ignore the others (derive_from,
  subject, artifact).
- **B. `task.kind=reasoning` as a lightweight task variant.**
  Rejected: bloats the task abstraction and violates the user's
  explicit constraint that tasks must stay light. Reasoning
  belongs to notes.
- **C. Single Block entity merging Note and Task** (Roam /
  Logseq pattern). Rejected: explicitly against the user's
  preference; would force task semantics (workflow, scheduling,
  billing) onto idea capture, or strip notes of their
  embedding/tag richness. Two entities with rich relations is
  the right shape for this user.
- **D. Promote = soft-delete the note** (it disappears once
  transplanted). Rejected at user request. The plant remains in
  its original spot; transplant is visible bidirectionally.
- **E. Single relation column on note + task** (one FK on each
  side, no link table). Rejected: cannot model multiple
  cardinality (a note has N derived tasks; a task has 1
  subject + 1 artifact + 1 promoted_from incoming). Typed M:N
  is the natural shape.
- **F. Tag-based clusters instead of an `atom_of` link**
  (a generic tag `inquiry:identity-refactor` groups notes).
  Rejected: tag is convention, not schema; loses referential
  integrity; the index-note pattern is structurally a link,
  not a label.
- **G. Maturity as soft-delete (only mature vs deleted)**.
  Rejected: collapses three useful states (seed, growing,
  mature) and loses the dormant→growing recovery path that the
  garden metaphor naturally suggests and that matches the
  user's described behaviour ("ideas that come back stronger").
