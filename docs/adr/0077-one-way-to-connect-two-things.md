# ADR-0077: One way to connect two things, notes or tasks, and the graph follows the work

Status: Accepted (2026-11-25, Angelo approved phases A and B as proposed). Phases A (2.4.25; the 2.4.24 tag failed its build and has no images) and B (2.4.26) implemented. Phase B was rewritten on 2026-11-25 before it was built: its first version assumed the retrieval walk included tasks, and it does not.
Relates to: ADR-0029 (typed note links), ADR-0040 (the four note-to-note
kinds), ADR-0042 (tasks in the unified graph), ADR-0073 (written references),
ADR-0075 (one kind of proposal), ADR-0004 (task dependencies).

This record decides how a person connects a note or a task to another note or
task: one notion of connection, the same panel and the same words on both
pages, one service contract over the existing tables, and the structure of the
work (subtasks, dependencies) as part of the unified graph of notes and tasks.
It records what was measured on 2.4.23, the decision, the phases, the
alternatives rejected, and one question left open: whether tasks should enter
the walk the memory uses for retrieval.

## Context

Angelo, 2026-11-25: connecting and disconnecting two notes is useful the way it
is for tasks; the connections between tasks, between notes and between notes
and tasks must be coherent, ordered and clear, and serve what Mycelium is for.

Measured on 2.4.23. Seven mechanisms connect two things:

| between | mechanism | kinds | in the memory graph |
|---|---|---|---|
| task and task | `task_relations` | related | yes, weight 0.45 |
| task and task | `task_dependencies` | FS, SS, FF, SF, with lag | no |
| task and task | `tasks.parent_task_id` | subtask | no |
| note and note | `note_note_link` | hypha_of, related, supersedes, contradicts | yes, 0.45 to 0.85 |
| note and task | `note_task_link` | subject, artifact, derived_from, promoted_from | yes, 0.50 to 0.70 |
| any two | `body_reference` (ADR-0073) | a mention written in a body | yes, 0.45 |
| note and note | `link_proposal` (ADR-0073) | proposed, not yet a link | no |

What a person meets:

- **Different places.** On a note there are three tabs ("Task", "Idee",
  "Suggerimenti") holding five panels. On a task there are four tabs
  (subtasks, dependencies, related, notes) and two more panels below them.
  Connecting two notes exists, in the "Idee" tab of the note, and was not
  found by the person who asked for it.
- **Different words for one act.** "Derived from" is `hypha_of` between notes,
  shown as "ifa di", and `derived_from` between a note and a task, shown as
  "derivato"; the promotion of a note to a task is "trapiantato". The panels'
  hints show internal references ("Quattro relazioni tipizzate fra questa nota
  e i task (ADR-0029)").
- **Direction left to the reader.** A row shows a kind and a title; whether
  this note derives from the other or the other from this one is not said.
- **The same connection in two lists.** A note mentioned in a body is listed
  under written references, apart from the typed links to the same note.
- **The graph does not see the structure of the work.** A subtask and its
  parent, or a task and the one it waits for, are the closest ties in a piece
  of work, and they are absent from the unified weave of notes and tasks
  (ADR-0042) that the graph view, the centrality of tasks and the
  suggestions of related tasks are computed on. (The weave the memory walks
  for retrieval and for an agent's reading set is notes only; tasks do not
  enter it at all, structure or not. Measured on 2026-11-25 in
  `graph_local` and `focus_context`.)

## Decision

### 1. One notion: a connection between two items

A connection links two items, each a note or a task. Every page shows them in
one panel, Connections, with the same layout on a note and on a task:

- grouped by meaning, not by table: what this item comes from, what comes from
  it, what it is related to, what it replaces or contradicts, and for a task
  the structure of the work (parent, subtasks, what it waits for, what waits
  for it);
- each row a sentence with its direction in words ("this note comes from
  X", "T was created from this note"), the other item's kind and title, and
  Disconnect where the connection can be removed;
- a mention written in a body is a row of the same list, marked as mentioned
  in the text, with no Disconnect: it is removed by editing the text;
- one Connect: pick any note or task, then one of the relations valid for
  that pair, each described in words.

Every word on the screen comes from the i18n catalogue (English and Italian
today); the relations below are codes, and their sentences are catalogue
entries. The garden's proposals of new connections stay on the proposals
page (ADR-0075), and a row there links to the panel of both items.

### 2. One vocabulary

A closed set of relations, each with one meaning and one name wherever it
applies:

| relation code | meaning | between | stored as |
|---|---|---|---|
| `related` | the two are connected, no direction | note-note, task-task | `related` (both tables) |
| `works_on` | the task is the work on the note | note-task | `subject` |
| `derives_from` | one comes from the other (a task that fell out of a note comes from it) | note-note, note-task | `hypha_of`, `derived_from` |
| `promoted_to` | the note became the task (read-only, as today) | note-task | `promoted_from` |
| `result_of` | the note is what the task produced | note-task | `artifact` |
| `supersedes`, `contradicts` | one makes the other obsolete, or false | note-note | as named |
| `subtask_of` | the task is part of the other | task-task | `parent_task_id` |
| `depends_on` | the task waits for the other (finish-to-start and the other three types, with lag) | task-task | `task_dependencies` |
| `mentions` | the body names the other | any two | `body_reference` |

No internal name or record number reaches a person's screen.

### 3. One contract over the existing tables

`services/connections` gives the seven mechanisms one row shape and the verbs
`link` and `unlink`, with an adapter per mechanism found by presence, as the
proposals of ADR-0075 do. Each table keeps its invariants: the dependency DAG
and its lag, the canonical pair of a related link, the decay a `supersedes`
sets, the promotion that cannot be undone by unlinking. The registry
operations `connections.list(item)`, `connections.link` and
`connections.unlink` serve the panel and MCP alike.

### 4. The structure of the work joins the unified weave

Subtasks and dependencies become edges of the unified weave of notes and
tasks, with a weight of their own (proposed: parent 0.70, dependency 0.55),
so the graph view, task centrality and the related-task suggestions see the
structure of the work. Whether tasks should also enter the walk the memory
uses for retrieval is a separate question, open below.

## Phases

| phase | delivers |
|---|---|
| A | decisions 1, 2 and 3: the panel on both pages, the vocabulary, the contract; the old tabs and panels removed |
| B | decision 4 |

### Phase A as built (2026-11-25)

| decision | where | checked by |
|---|---|---|
| 1, one panel | `web/src/components/ConnectionsPanel.tsx` on the note page, the task page and the garden's note view; the note page keeps a second tab for the garden's suggestions; the tabs and panels it replaces (`NoteLinksPanel`, `LinkedTasksPanel`, `LinkedNotesPanel`, the task page's subtasks, dependencies and related tabs, the garden modal's link lists) are removed; ambiguous mentions keep their chooser, shown only when there is one to resolve | `ConnectionsPanel.test.tsx`, observed failing with the direction dropped from the grouping; `web/e2e/connections.spec.ts` (connect two notes from one page, read the direction from both, disconnect), observed failing with the two directions' names swapped |
| 2, vocabulary | `connections.Relation`, codes in English; every sentence and group name comes from the i18n catalogue (`itemConnections`, en and it); the interface strings that showed record numbers no longer do | `check:i18n` |
| 3, one contract | `services/connections/`: an adapter per mechanism (`note_notes`, `note_tasks`, `task_related`, `task_parent`, `task_dependencies`, `mentions`) found by presence, each declaring the (relation, from, to) it creates, so the valid connections have one source; `connections.list`, `.connect`, `.disconnect` on REST (`/connections`) and MCP (`list_connections`, `connect_items`, `disconnect_items`); each kind of item needs its own scope, and a connection to a kind the caller cannot read is left out | `core/tests/test_connections.py`, `api/tests/test_connections_api.py` |

Found on the way and fixed at its origin: `update_task` accepted any
parent, so a task could become its own parent or the parent of its parent;
it now refuses with `task.parent_cycle` (observed failing without the
check). The MCP gateway expands short ids by argument name, and `item_id`
and `other_id` already meant a checklist item and a task there, so the
operations take `this_*` and `that_*`, expanded by the kind named beside
each.

### Phase B as built (2026-11-25)

`graph.work_structure_pairs` reads the structure of the work once, as
undirected pairs: (subtask, parent), and dependencies with a pair joined by
several types counted once. Both surfaces that build the unified weave read
it: the edge weights (parent 0.70, dependency 0.55; feeding PageRank,
betweenness and the clusters) and the personalised walk, which keeps its own
neighbour list and, before this, would have missed edges added to the
weights only. The snapshot signature folds in each set as a count and an
order-independent sum of a hash of its pairs, so a re-parent with every
count unchanged invalidates the stored analytics. The related-task
suggestions no longer propose the source's parent, subtasks or
dependencies, which are already connected; the walk still passes through
them to the tasks beyond. With the unified weave off nothing changes.

Checked by `core/tests/test_work_structure_graph.py`: observed failing with
the exclusion removed, with the signature reduced to counts, and, before the
walk read the shared pairs, on the task beyond a dependency.

## Alternatives rejected

- **One physical table for every connection**: the dependency DAG, its lag
  and its scheduling, the canonical related pair and the promotion invariant
  would become optional columns of one table, each checked by hand; a contract
  over the existing tables keeps every rule where it is enforced today.
- **Rename the labels and keep the panels**: the words would agree, the
  places would not, and the person would still have to know which tab holds
  which kind of connection.
- **Turn written mentions into typed links**: a mention is what the person
  wrote, not a claim about the kind of relation (ADR-0073); it is shown beside
  the links, not converted into one.

## Open

Whether tasks, and with them the structure of the work, should enter the
walk the memory uses for retrieval and for an agent's reading set. Today
that walk is notes only (`graph_local`, `focus_context`); widening it
changes what a search returns, and would be decided by a retrieval
evaluation that includes tasks, which does not exist yet.

## What this does not cover

- Which relations the garden proposes and how it scores them (ADR-0073).
- The scheduling meaning of dependencies (ADR-0004): unchanged.
