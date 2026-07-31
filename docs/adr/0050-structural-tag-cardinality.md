# ADR-0050: Structural tag cardinality on tasks and notes

Status: Accepted (2026-07-31)
Revises: ADR-0003 (unified tag with typed satellite profiles), which
defined the tag model and said "one relation per kind" in passing but
was silent on cardinality, on who owns it, and on what happens when a
client and a project contradict each other.
Relates to: ADR-0007 (hard memory isolation per (org, project)),
ADR-0021 (explicit scope, never a silent mis-scope), ADR-0002
(tenant invariants enforced in the schema, not only in the service
layer), migration `0016` (`notes.project_id` dropped, the junction is
the only truth), `core/tests/test_f6b_notes.py` (share/un-share by
project tag).

## Context

ADR-0003 made client and project *kinds of tag* and moved their
structured data into satellite profiles. It never said how many of
each an entity may carry, so the junctions (`task_tags`, `note_tags`)
accepted anything the caller wrote: zero clients, three clients, a
project tag whose `project_profile.client_tag_id` contradicted the
client tag sitting next to it. Every door (HTTP, MCP, CLI, importers,
the auto-classifier) re-derived its own partial rules, and the ones
that wrote junction rows directly had none.

This is not a tidiness problem. The (org, project) perimeter of
ADR-0007 is *derived* from these rows: an entity carrying two clients,
or a project belonging to a different client than the one attached,
has no well-defined perimeter, and isolation degrades to whichever row
a given query happens to read first. `memory_channel` had meanwhile
become the fourth tag kind, and every doc still described `kind` as a
three-value enum.

## Decision

Four kinds: `generic | client | project | memory_channel`. `generic`
and `memory_channel` are free-form facets and stay unconstrained
many-to-many. `client` and `project` are **structural** and obey:

- **(a)** a TASK carries EXACTLY ONE tag of kind `client` and EXACTLY
  ONE of kind `project`;
- **(b)** a NOTE carries EXACTLY ONE `client` and AT MOST ONE
  `project`;
- **(c)** if an entity carries a project tag, its client tag IS that
  project's `project_profile.client_tag_id`;
- **(d)** every project has exactly one client:
  `project_profile.client_tag_id` is NOT NULL.

The asymmetry between (a) and (b) is deliberate and must not be
"fixed" into symmetry. A projectless note is not a note missing a
field: it is a first-class retrieval perimeter, the personal one, and
it is indexed with `memory_blobs.project_id` NULL (ADR-0007's scope
made explicit by ADR-0021's "personal inbox" default). Attaching a
project tag re-scopes its blobs into the project with no content edit;
detaching sends them back to personal. Both directions are guarded by
`core/tests/test_f6b_notes.py`. A task has no such perimeter: it always
resolves to a project, falling back to the workspace default
("General" under the "Personal" client, `taxonomy.ensure_default_project`).

On the two contradictory moves, the outcome is asymmetric because the
project is the truth and the client is derived from it (c):

- Attaching a PROJECT whose client differs from the entity's current
  client is a **MOVE**, not an error: the client tag is atomically
  replaced by the project's client. Moving work between clients is a
  normal act, not a mistake to report.
- Attaching a CLIENT that contradicts the attached project is
  **REJECTED** (`TAG_CLIENT_PROJECT_MISMATCH`). The client carries no
  information the project does not; honouring it would either break
  (c) or silently drop the project the user never asked to drop.
- Detaching: a task's client or project is rejected
  (`TAG_STRUCTURAL_REQUIRED`, by (a)); a note's client is rejected; a
  note's project is allowed (it is the un-share path) and rescopes the
  blobs.
- `taxonomy.update_project` reassigning a project's client re-tags
  every dependent task and note **synchronously, in the same
  transaction**.
- `POST /tags` and the MCP `create_tag` reject `kind` `client` or
  `project`. `/clients` and `/projects` are the only doors, because
  they are the only ones that create the satellite profile, and a
  structural tag without its profile cannot satisfy (c)/(d).

Enforcement is a choke point:
`core/src/mycelium_core/services/tag_assignment.py` is the only code
in the tree allowed to INSERT or DELETE a `task_tags` / `note_tags`
row of kind client or project, with a DB-level guard underneath as
defense in depth (ADR-0007's lesson: an application-only rule is one
forgotten call site away from a leak).

## Consequences

- `memory_blob_tags` is deliberately OUT of scope. Consolidation
  unions the member blobs' tags by design: a consolidated blob is
  evidence of what it was built from, and truncating that union to one
  client and one project would erase provenance. A blob's authoritative
  perimeter is the scalar `memory_blobs.project_id`, not its tag rows,
  so the union is safe. The price, accepted knowingly: the same tag
  picker in the UI carries two different rules (constrained on a task
  or a note, free on a blob), and the widget must say so rather than
  be unified later.
- A structural chip on a task is never "just removed": the UI offers
  *move* (pick another project) where the API rejects a bare detach.
- Any ingest path (importers, auto-classify, email -> task, voice
  notes) must route through `tag_assignment`. A direct
  `session.add(TaskTag(...))` is now a bug, not a shortcut.
- A client reassignment on a project costs O(dependent tasks + notes)
  inside the caller's transaction. Accepted: a rare admin action pays
  for the perimeter being correct at commit.
- Pre-existing rows violating (a)-(d) are repaired by migration; the
  repair runs under the NO FORCE / try / restore FORCE bracket
  documented in [migrations.md](../migrations.md).

## Alternatives rejected

- **Constraining `memory_blob_tags` the same way.** Kills consolidation
  provenance for no isolation gain: the perimeter is the scalar column.
- **Symmetry: require a project on notes too** (defaulting to
  "General"). Destroys the personal perimeter, silently moving every
  personal note into a shared project: the exact mis-scoping ADR-0021
  refuses.
- **Rejecting a cross-client project attach instead of moving.** Under
  (a) the user cannot detach first, so the operation becomes
  unreachable; and the intermediate state has no legal representation.
- **Asynchronous re-tag after `update_project`** (job or outbox). It
  opens a window in which (c) is false, i.e. a window in which the
  memory perimeter of those entities is ambiguous. ADR-0007 does not
  admit windows.
- **Enforcing in each router.** That was the status quo: several doors,
  several partial rules, and the write paths that never went through a
  router bypassing all of them.
