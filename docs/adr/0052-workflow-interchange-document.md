# ADR-0052: The workflow interchange document is JSON, and carries no database identity

Status: Accepted (2026-08-23)
Relates to: FR-6 (`docs/functional-requirements.md`, configurable state
workflows: one default per Org, optional per-project override),
ADR-0002 (multi-tenant, optimistic concurrency, RLS), ADR-0017
(English-only project language, i18n-ready catalog),
`core/src/mycelium_core/models/workflow.py`,
`core/src/mycelium_core/services/workflow.py` (`update_workflow`
reconciles states BY ID),
`core/src/mycelium_core/services/workflow_io.py` (the only
implementation of this format).

## Context

The workflow editor could create, edit and delete a definition, but a
definition could never leave the workspace it was born in. Moving a
lifecycle that took real thought to design (states, terminal flags,
hidden columns, the state descriptions MCP agents read) from a staging
workspace to production, or between two of a consultant's clients, meant
retyping every row and hoping the transition matrix came out the same.

The editor's Save/Cancel pair now has an Export/Import pair beside it,
and `mycelium workflow export|import` does the same job from a
terminal. That forces three decisions that outlive the buttons: what
the file contains, what "import" means when the target already exists,
and WHERE the rules of the format live given that two unrelated clients
need the same answer.

Two properties of the domain constrain them.

First, everything the editor touches is org-scoped and identified by
UUIDs allocated in one database: `workflow_defs.id`, `workflow_states.id`,
`org_id`, and an optimistic-lock `version`. None of them mean anything in
another workspace, and the ones that do resolve there resolve to some
other org's rows.

Second, `PATCH /workflows/{id}` reconciles states **by id**. A state row
arriving without an id is an insert; an existing state absent from the
payload is a delete, and the service refuses the delete outright if any
task still sits in that state (`WORKFLOW_IN_USE`). Tasks point at
`workflow_states.id`, not at a name.

## Decision

**D1 -- The format is JSON, not XML.** The whole stack is already JSON:
the OpenAPI contract, `openapi-fetch`, and the `POST /workflows` and
`PATCH /workflows/{id}` bodies this document is a near-copy of. A
workflow is a set of flat records and a list of edges, not a document
with mixed content, so JSON represents it without the element-versus-
attribute choice XML would force. `JSON.parse` needs no dependency;
useful XML validation would mean shipping an XSD, and without one the
validator gets hand-written anyway. XML in this repo exists for exactly
one reason and it is not interoperability in general: FatturaPA, where
the Agenzia delle Entrate dictates the schema.

**D2 -- The document carries no database identity.** No workflow id, no
state ids, no `org_id`, no `version`, no `is_default`. A state is
addressed by its NAME and a transition names the two states it joins,
exactly as the API bodies already do. This is what makes a file exported
from one workspace importable into another.

**D3 -- The position of a state in `states` IS its `ord`.** The editor
already derives `ord` from the row position when it saves. Writing it
into the file as well would create two sources of truth free to
disagree.

**D4 -- Export and import are SERVER operations, and the rules live in
the service layer.** `GET /workflows/{id}/export`,
`POST /workflows/import` and `POST /workflows/{id}/import`, all three
implemented once in `services/workflow_io.py`. The SPA and the CLI are
two clients of those endpoints and neither parses a document itself: a
second implementation would be a second answer to "is this file valid",
and the two would drift. The writes delegate to `create_workflow` /
`update_workflow` rather than touching the tables, so the RBAC check,
the invariants, the refusal to delete an occupied state and the audit
entry are unchanged -- an importer that bypassed them would be a
second, weaker door onto the same data. An import adds its own
`action="import"` audit row recording which states were kept, added and
dropped, because "this arrived as a file" is what an operator reading
the log later needs to know.

The consequence, which the UI has to state plainly: **import writes
immediately**. There is no "press Save to apply" step and Cancel does
not undo it, so the SPA asks for confirmation first. Export downloads
the STORED workflow, so unsaved edits in the panel are not in the
file.

**D5 -- Import into an existing workflow matches states BY NAME and
keeps their ids.** A state named again by the document keeps the id it
already had, so the save updates that row in place and the tasks sitting
in it never move. A name the workflow did not have arrives without an id
and is inserted. A state the document does not mention is a deletion,
which the backend still refuses while tasks occupy it. A rename is
therefore a delete plus an insert, which is the honest reading: the
document gives no other way to say "this is the same state under a new
name", and silently reusing an id would rename the state every finished
task points at.

**D6 -- The reader refuses rather than coerces.** Exactly one initial
state; names trimmed, then unique and
within the `String(120)` / `String(80)` column widths; every transition
endpoint resolvable; no duplicate edge (the `(workflow_id, from, to)`
unique constraint would otherwise surface as an unhandled
`IntegrityError`). A flag that is neither boolean nor absent is refused,
not read as false. Each refusal is a distinct tagged error naming the
offending row, rendered through the i18n catalog.

**D7 -- Unknown keys are ignored; an unknown `version` is refused.** The
document is stamped `{"kind": "mycelium.workflow", "version": 1}`.
Adding an optional field does not bump the version; only a change the
current reader would misread does.

**D8 -- Export refuses to write a file the importer would reject.**
`export_workflow` runs the stored rows through the same `normalize` an
import goes through, so the two sides cannot drift apart and a workflow
the exporter cannot describe legally surfaces here rather than on the
machine the file was carried to.

## Consequences

- A workflow moves between workspaces, and between environments, as a
  file. Nothing about it is tied to the database it came from.
- Re-importing an exported file over its own workflow is a no-op edit:
  every state name matches, so every id is kept and nothing is deleted.
- Renaming a state through the file is a delete plus an insert, and is
  refused while tasks occupy the old state. Renaming stays a job for the
  editor's text field, which edits the row in place. This is a real
  limitation, accepted as the price of D2.
- `is_default` does not travel. It is a property of the workspace's
  configuration, not of the workflow, and `create_workflow` hardcodes
  `is_default=False`; promoting an imported workflow stays a separate,
  audited action.
- Three new routes, three new `ROUTE_SCOPES` entries. The RBAC surface
  is unchanged in substance: the two writes are `workflows:write` and
  need the owner role like every other workflow write, the export is
  `workflows:read` and a member may take one.
- A third client (an MCP tool, an importer in the worker) gets the
  format for free by calling the same three service functions. Any
  client that restates the rules instead breaks D4.
- Structural errors (a string where a boolean belongs) come back as
  FastAPI's 422 validation envelope, semantic ones as a
  `{code, detail}` domain error; both clients already render both
  shapes. The schemas are declared `strict` so pydantic cannot repair
  `"is_terminal": "true"` into `True` on the way in.
- `GET /workflows/{id}/export` on an id from another workspace answers
  400 `workflow.not_found`, not 404: `WORKFLOW_NOT_FOUND` is a plain
  `DomainError` throughout this service, and the new route follows the
  surface it belongs to rather than being the one that answers
  differently.

## Alternatives rejected

**XML.** Rejected under D1. It would buy schema validation only by also
shipping and versioning an XSD, and would leave every reader in this
codebase parsing a DOM to reach data that is already JSON everywhere
else in the app.

**Export the ids and match on them.** Makes a file meaningful in exactly
one database. An import into any other workspace would address rows that
either do not exist or belong to someone else, which is precisely the
failure mode RLS exists to prevent. Rejected outright.

**Doing it in the SPA, with the file parsed in the browser.** This was
the first implementation, and it was wrong for a reason worth writing
down: it made the feature unreachable from anything that is not the
web app. A workflow that can only be moved by a human with a browser
open is not portable in the sense that mattered. Reversing it also
removed the staging step the browser version had (import filled the
form, Save applied it), which was pleasant and is genuinely lost --
paid for the fact that `mycelium workflow import` exists at all, and
that there is exactly one definition of a valid document.

**Coerce a malformed file instead of refusing it.** A `"is_terminal":
"true"` read as false, or a missing initial state defaulted to the first
row, produces an import that looks like it worked and a lifecycle that
is quietly wrong. The backend would accept most of it. Refusing with the
row number costs one error type per rule and is the only version a user
can act on.
