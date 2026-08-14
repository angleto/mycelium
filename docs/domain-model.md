# Domain model

Conceptual entities. The physical schema is in
[data-model.md](data-model.md).

- **Workspace** (the user-facing name; internally **Organization**,
  `org_id`, RLS unchanged, ADR-0024/ADR-0015): the tenancy boundary.
  Personal-first: a personal workspace is auto-provisioned at signup;
  a user may belong to several and switches in-app without re-auth.
  Owns an **issuer fiscal profile** (RegimeFiscale RF01.., VAT/CF,
  structured address, REA,
  cassa) needed for invoicing. Everything is org-scoped.
- **User / Membership / Role**: users, membership in one or more Orgs,
  RBAC (owner, admin, member, read-only guest).
- **Tag** with `kind` in {generic, client, project, memory_channel}. A
  single label concept. Legal/fiscal and billing data does not live in
  free JSONB but in typed satellite profiles with an FK to `tag.id`:
  - `client_profile`: legal name, IdFiscaleIVA (country + id), tax
    code, structured address, recipient code or PEC.
  - `project_profile`: reference to the client tag (parent, NOT NULL:
    every project has exactly one client), rate, currency, budget,
    optional workflow override.
  Associating a client/project with a task or a note = attaching the
  relevant tag, but `client` and `project` are **structural** kinds:
  the junction is cardinality-constrained, not free many-to-many. A
  task carries exactly one client and exactly one project; a note
  exactly one client and at most one project (no project = the
  personal retrieval perimeter, ADR-0007/ADR-0021); the client is
  always the attached project's own client. Attaching a project of a
  different client MOVES the entity; a client contradicting the
  attached project is rejected. `generic` and `memory_channel` stay
  unconstrained many-to-many. ADR-0003, ADR-0050.
- **Task**: the primary unit. State (from the workflow), priority,
  `estimate_effort_h`, `remaining_effort_h`, `actual_start`,
  `is_milestone`, **`executor`** (human user or LLM agent), subtasks,
  tags, comments, attachments. Personal-planning attributes:
  `monetary_cost?`, `location?`, `necessity` (must/should/nice),
  context/preconditions via generic tags (e.g.
  `ctx:requires-computer`, `place:hardware`), `budget_id?`. A task
  belongs to exactly one project, hence to exactly one client
  (ADR-0050): either a personal project (a non-billable project under
  the default "Personal" client) or a client project, never both. With
  none stated it lands on the workspace default project ("General").
- **TaskDependency**: a typed directed edge (FS/SS/FF/SF) with `lag`
  (signed working minutes; reference calendar = the predecessor). The
  set is a DAG; cycle detection is mandatory.
- **Appointment**: a Task with `start_at` (timestamptz) +
  `duration_minutes` (int) IS the calendar block (migration 0094 /
  ADR-0008 addendum). Extra participants live on
  `task_participants(task_id, identity_id, start_at,
  duration_minutes)`. Constraint: no overlap per identity
  (no-ubiquity), enforced by a single GiST EXCLUDE on
  `task_participants`; the 0096 trigger mirrors the task's
  `assignee_id` into a participant row so the EXCLUDE covers both
  the primary owner and every additional invitee.
- **WorkflowDefinition**: ordered states and transitions; default per
  Org; an override attachable to a `project_profile`.
- **WorkingCalendar**: weekly hours, holidays, timezone; Org default +
  per-user override (daily capacity).
- **TimeEntry**: from a timer or manual; billable; rate snapshot; feeds
  `remaining_effort` and the invoices.
- **EmailAccount / EmailMessage**: Gmail OAuth2, Proton Bridge, generic
  IMAP connector; messages for triage with a link to the created Task.
- **MemoryBlob + BlobSource**: hierarchical memory (hot/warm/cold
  tiers) with explicit N:1 provenance for GDPR deletion; (org, project)
  scope.
- **Invoice**: a state machine (draft, issued/transmitted, SdI
  terminal); immutable after emission; correction only via a TD04
  credit note.
- **SdiMandate**: per-Org authorization to transmit on its behalf
  (scope, validity, revocation, audit).
- **PaymentConnector**: a per-issuer-profile inbound connector that turns a
  payment provider's events into fiscal documents (ADR-0051). It holds the
  credentials the sender authenticates with (a signing secret, an optional
  ingress key), the automation posture (emit and file / emit as draft /
  record only, independently for invoices and credit notes) and the fiscal
  defaults that complete what the provider does not say. Two providers are
  understood: `stripe` (an adapter over someone else's event shape) and
  `mycelium`, our own published contract
  ([payment-connector-contract.md](payment-connector-contract.md)), so a
  sender with no adapter can integrate against a documented format. The
  provider's vocabulary never leaves the adapter: everything downstream
  speaks neutral events (emit / credit / payment). Around it live three
  ledgers, conceptually distinct: the **connector event** (one per provider
  event, frozen payload, the durable ingress record AND the work queue,
  ending `done` / `ignored` / quarantined in `needs_attention` / `dead`),
  the **object link** (a provider object id -> the document emitted for it:
  what makes emission idempotent and what lets a refund find its parent),
  and the **delivery** (one per inbound HTTP attempt, refusals included,
  keeping the body's digest and not the body). Beside them sits the
  connector's own identity map, provider customer -> client tag, which
  keeps repeat business on a single client. A connector is also an actor:
  documents it emits are attributed to it in the audit trail.
- **ConservationRecord**: the compliant-conservation status per invoice
  and per SdI receipt ("free AdE service" model).
- **Budget**: an org-scoped spending envelope per period and category
  (e.g. home expenses) with an allocatable amount; tasks with
  `monetary_cost` consume it.
- **PlanningQuery (advisory capability, not a persistent entity)**: a
  service-layer capability = feasibility filter + ranking + constrained
  selection (priority knapsack) over tasks accessible to the user
  within an org. LLM/MCP as the natural-language frontend. Does not
  violate memory isolation (ADR-0007), which governs RAG/email content,
  not the user's task list.
- **Notification / NotificationPref**: a message on a channel
  (Telegram/email) and per-user, per-event preferences.
- **Comment / Attachment / ActivityLog**: collaboration and audit
  (append-only log).
