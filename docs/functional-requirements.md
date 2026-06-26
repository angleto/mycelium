# Functional requirements

## FR-1 Task management

Task CRUD (title, markdown description, P1-P4 priority, start/due
dates, assignees, `executor`), hierarchical subtasks, checklists,
quick-add with natural parsing (e.g. `tomorrow #tag @project !p1`),
list/board/calendar/today-upcoming views, saved filters, full-text
search, completion, archiving, soft-delete with restore, comments,
activity log, attachments. Recurring tasks and reminders (FR-12). Tasks
also carry personal-planning attributes (cost, location, context,
necessity): see FR-14.

Carve-out: soft-delete does **not** apply to issued invoices and
conserved documents (see FR-9 and ADR-0009).

## FR-2 Unified taxonomy

A single `tag` with `kind`. `client` and `project` have typed satellite
profiles (constraints, validation, FK), not free JSONB: needed both for
invoicing (legal data) and for per-project memory isolation. A single
task-tag association. RBAC and org-scope on all tags.

## FR-3 Dependencies and workflow graph

Four dependency types (FS/SS/FF/SF) with lag/lead; semantics in the
working-time inequalities (FR-4). Cycle detection before insertion, in
the service layer. DB constraints: no self-dependency, unique
(predecessor, successor, type), same org. DAG graph viewer (pan/zoom,
click opens the task, dagre/elkjs layered layout). "Blocked" is a
non-persistent derived overlay, not a workflow state.

## FR-4 Deterministic scheduling (complete from the start)

Resource model:

- each person is a unary resource on their own calendar;
- appointment-tasks (tasks with `start_at` + `duration_minutes`,
  migration 0094 / ADR-0008 addendum) are exclusive fixed
  reservations: their position is pinned and they never enter the
  placement loop. Every participant (assignee mirror + explicit
  invitees) has the slot in their per-person busy list;
- the same person's `executor=human` work tasks are **serialized**
  (no time overlap) around these appointment-tasks;
- `executor=llm_agent` tasks are off the human timeline (parallel,
  scheduled by precedence only).

Engine: a deterministic **logical** CPM forward/backward pass over
working calendars (ES/EF/LS/LF, slack, logical critical path, honest
because contention-free) + **deterministic per-person serial
placement** with a priority rule (priority desc, due asc, created asc,
id) into the free windows. Stable output: pins survive recompute.

Plan vs actuals: `remaining_effort_h` (default = estimate),
`actual_start` (from the first time entry or a state transition); a
terminal-state task = zero residual duration; an in-progress task = ES
pinned to `actual_start`, only the residual is scheduled.

Mode/pin: `schedule_mode` {auto (ASAP), manual} + `constraint` {none,
SNET, MSO, MFO}. Drag with write-back sets manual or a constraint and
the recompute respects it.

Determinism: given the input, identical output; a derived `schedule`
with `computed_at` + `input_fingerprint` + a staleness flag; the most
recent recompute supersedes the previous.

Rollup: effort on the leaves; the summary task derives start = min,
finish = max of the children; in v1 dependencies only on leaves.

Gantt: bars, dependencies, logical critical path, milestones, a
per-person/day overload indicator, drag.

Working-time inequalities (lag in signed working minutes, the
predecessor's calendar):

- FS: `start_succ >= finish_pred + lag`
- SS: `start_succ >= start_pred + lag`
- FF: `finish_succ >= finish_pred + lag`
- SF: `finish_succ >= start_pred + lag`

Optimizing leveling (CP-SAT) is a post-v1 enhancement, not the default.
See [ADR-0004](adr/0004-deterministic-scheduler.md).

## FR-5 Time tracking

Live timer (one running per user, start/stop/resume), manual entries,
client-side idle detection, the running timer visible in GUI and MCP
in realtime, reports per client/project/generic tag/user/period,
billable, rates, CSV/PDF export. Feeds `remaining_effort` (FR-4) and
the invoices (FR-9).

## FR-6 Configurable state workflows

WorkflowDefinition per Org (one default): ordered states, transitions,
initial/terminal. Project override: a `project_profile` points at a
workflow that extends the default (e.g. adds a state and mandates the
transition). State-machine enforcement in the service layer (the
single GUI/REST/MCP choke point).

## FR-7 Email: connector, triage, send

- Gmail: OAuth2 (XOAUTH2 IMAP/SMTP), encrypted tokens, refresh.
- Proton Mail: via Proton Mail Bridge (paid plan), headless arm64
  sidecar; one instance per account, controller at scale; opex
  documented, not a blocker; introduced after Gmail.
- Generic IMAP/SMTP: self-hosted/Dovecot, basic or OAuth.
- Idempotent, resilient background sync; one account's failure does not
  degrade the rest.
- "Email to Task" from GUI or an MCP tool: title from subject,
  description from body, attachments carried over,
  tag/client/project/assignee assignment, a link to the source
  message.
- SMTP send/reply in the thread. No server-side folder/label
  management in v1.

## FR-8 Hierarchical memory and retrieval

- Hard isolation per (org, project): `memory_blobs` partitioned by org,
  mandatory (org, project) predicate, mandatory RLS. Default = current
  project; cross-project only with explicit, audited authorization. No
  retrieval/summarization/consolidation crosses projects or tenants.
- Explicit `blob_sources` provenance (N:1). Consolidation only within
  the same (org, project, thread/account), never cross-subject;
  tombstone, not silent delete.
- GDPR erasure: deleting a message propagates to embedding, summary,
  the object-storage copy and every consolidated blob that includes it
  (re-consolidation from survivors or a tombstone).
- Tiers: hot (body in PG on an encrypted volume), warm (LLM summary if
  enabled, body to object storage), cold (pgvector HNSW embedding).
  With the LLM disabled: warm = body to object storage + metadata, no
  summary.
- Human-like tiering (ADR-0016): the tier is driven by an access score
  with decay (frequency + recency) and an importance signal, not just
  age/size. Recurring concepts (consolidated clusters, preserved
  provenance, within (org, project)) are promoted to a compact pre-warm
  tier; what does not recur decays and is demoted. **Invariant**:
  frequency determines only the latency tier, never retention nor
  visibility; cold stays always retrievable (rare != not important).
- Corrective grader (adapted CRAG, ADR-0016): every retrieval is graded
  (a deterministic threshold on the fused RRF score + an optional local
  LLM grader). Branches: ok -> use; uncertain -> rewrite/expand the
  query; insufficient -> widen the scope within the tenant or answer
  "insufficient evidence". No web branch (private memory, GDPR).
- Retrieval as an agentic tool (ADR-0016): memory retrieval is exposed
  as an MCP tool among the deterministic tools; the LLM/MCP planner
  chooses vector/SQL/structured. Deterministic decision
  (ADR-0004/0013): the LLM orchestrates, it does not decide.
- Connections: use the structural graph already present (dependency
  DAG, tag/client/project hierarchy, email-task link and provenance),
  not an LLM-extracted knowledge graph from text (textual GraphRAG
  deferred).
- Multimodal deferred: v1 text-first with text extraction from
  attachments; multimodal embedding (CLIP/ColPali, single index) as a
  later phase.
- Baseline hybrid retrieval: a semantic branch (HNSW) + lexical
  (tsvector/ts_rank, pg_trgm with a dedicated trigram index). Metadata
  pre-filter = relevance; RLS + partition = security. For very
  selective filters (message-id, invoice number) an exact path, not
  HNSW. `hnsw.iterative_scan = relaxed_order` tuned.
- RRF: K oversampled per branch (around 100), RRF fusion with k around
  60, deterministic tiebreak (single rank, then received_at, then id),
  final N; fusion within (org, project).
- Pluggable Embedder/LLM (bitvision_phoenix pattern: Protocol +
  DB-driven factory + neutral DTOs + env settings + model registry).
  Mycelium adds `EmbedderProvider`. Default a small multilingual CPU/ARM
  model; concrete choice at implementation (BGE-M3, multilingual-E5,
  GTE-multilingual, Qwen3-Embedding on the current MTEB).
- Re-embedding: a new column/table for the new model (fixed dim),
  dual-write during the backfill, resumable backfill, `CREATE INDEX
  CONCURRENTLY`. Honest guarantee: no write downtime; reads on the old
  index during the backfill with possible latency degradation on a
  single node; cutover = swap of the pointer in a transaction;
  rollback while the old column+index exist.
- "No numpy" = no app-side numpy similarity store; vectors live in
  pgvector server-side (the local `Embedder` may depend on numpy
  transitively).

## FR-9 SDI electronic invoicing (v1 B2B/B2C)

- Legal role: in multi-tenant, Mycelium transmits on the tenant's behalf
  and is therefore a transmitter/intermediary. An explicit per-Org
  `SdiMandate` model. A single shared channel; the tenant's identity in
  the payload (`CedentePrestatore`,
  `TerzoIntermediarioOSoggettoEmittente`), never in the TLS identity.
- The channel behind the `SdiChannel` abstraction:
  - `ManualExportChannel` (immediately): downloadable XML; invoices
    issued this way are already legally issued.
  - `SdICoopChannel` test: an always-on inbound SOAP endpoint exposed
    by Mycelium (SdI pushes, not polling), server-side mutual TLS;
    interoperability tests.
  - `SdICoopChannel` production: after the service agreement and
    accreditation (a heavy item, not a minor final step).
- Compliant conservation (obligation art. 39 DPR 633/72, 10 years):
  strategy = free AdE service. `ConservationProvider =
  AdeFreeConservation`: Mycelium does not conserve in-house, it tracks and
  guides per-tenant adhesion in the tax portal; AdE conserves only what
  passes through SdI; invoices from `ManualExportChannel` in F7a are
  marked "out of AdE coverage, the tenant's responsibility". Effective
  coverage from F7b.
- Signature: v1 B2B/B2C unsigned (not required via an accredited
  channel). PA/B2G (CAdES/XAdES-BES + qualified certificate) deferred
  post-v1.
- Outcomes: push SdI notifications on the inbound endpoint, correlated
  by `IdentificativoSdI` (a first-class indexed column). v1 active
  cycle: RC, MC, NS, AT. NE/DT/EC/SE and the passive cycle deferred
  with PA.
- Minimal v1 fiscal profile: TD01/TD04, standard rates + a reduced
  Natura set, optional withholding, stamp duty as a flag + manual
  quarterly export. Deferred: PA/split payment, reverse
  charge/self-billing TD16-TD19, foreign clients, quarterly stamp-duty
  settlement, advanced social-security fund.
- Immutability: a state machine; only `draft` is deletable; issued =
  append-only; correction only via a TD04 credit note.
- Numbering: progressive per (Org, series, year), concurrency-safe
  (sequence or `FOR UPDATE`), allocated only at the
  draft -> transmitted transition in the same transaction; never
  reused.
- History search; mark paid (manual reconciliation in v1); TD04 credit
  note.
- Implemented (F7b, config-gated on `MYCELIUM_SDI_CHANNEL=sdicoop`): official
  FatturaPA XSD validation at transmit (`Schema_VFPA12_V1.2.3`); the per-issuer
  `SdiMandate` + `TerzoIntermediarioOSoggettoEmittente` /
  `SoggettoEmittente=TZ`; the SdICoop `RiceviFile` SOAP transport (mutual TLS) +
  `/sdi/notification` inbound receiver (cross-org correlation by `IdentificativoSdI`
  via a SECURITY DEFINER resolver). Fiscal conformance vs the v2.6 specs: the
  VAT id is stored split (`IdPaese`/`IdCodice`) with country-prefix
  normalization and a mandatory cedente P.IVA; `RiferimentoNormativo` per issuer
  (RF19 default); persona fisica `Nome`/`Cognome` (Anagrafica choice);
  `Provincia` validated against the Italian list. External (F7c): accreditation
  + certificates + WSDL/esito verification against the AdE test environment.

## FR-10 MCP server (co-equal)

Exposes the domain as MCP tools, **the same service layer, RBAC,
(org, project) isolation** as REST. Auth as a user in an Org + project
(scoped token), idempotency on mutating tools, optimistic conflicts =
409. stdio transport (local Claude) and HTTP/SSE (remote). Per-project
memory isolation matters especially here.

## FR-11 Multi-user, auth, RBAC, concurrency

Signup/login, JWT, multiple Orgs per user, invitations, roles. RBAC in
the service layer. Mandatory RLS on org-scoped entities (primary
defense, not optional). Optimistic concurrency: `UPDATE ... WHERE id
AND version`; 0 rows -> 409; no silent lost update; append-only
activity log; realtime invalidation via WebSocket. A model fit for
future collaboration.

## FR-12 Notifications, recurrences, reminders

A channel abstraction with adapters: Telegram and email in v1, others
prepared. Deadline reminders, recurring tasks materialized by the
worker (instances = independent task rows; in v1 recurrence and
dependencies are mutually exclusive), event notifications (assignment,
block, no-ubiquity, SDI outcome). Per-user, per-event preferences.

## FR-13 Planning assistant (advisory, v1 core)

First-class advisory capabilities in the service layer, exposed via
REST + MCP tools, with the LLM/MCP as the natural-language interface
and the decision core **deterministic and explainable** (consistent
with ADR-0004). Three archetypes:

- **What-can-I-do-now**: given a free interval (start, duration,
  location/context, optional energy), returns the feasible tasks,
  ordered. Feasibility = fits the window (`remaining_effort_h` vs
  duration), not blocked by dependencies, `executor=human` and the
  user available (no conflict with events/no-ubiquity, FR-4/ADR-0008),
  compatible location/context. Deterministic ranking by
  urgency/priority/necessity/value.
- **Errand/context**: given a place or context (e.g. "I'm going to the
  hardware store"), aggregates the relevant items (tasks with a
  compatible `location`/context) across the projects accessible to the
  user within the org.
- **Prioritization within budget**: given a `budget` (envelope), a
  constrained selection (priority/value-density knapsack, must-have
  first) of the tasks/items with `monetary_cost` that maximizes value
  within the amount, with an explanation.

Verifiable determinism (same input, same output). Isolation: operates
on the tasks accessible to the user within an org (even multi-project);
not a memory-isolation violation (ADR-0007), which governs RAG/email
content. See [ADR-0013](adr/0013-planning-advisory-layer.md).

## FR-14 Personal domain and budget

- Tasks carry `monetary_cost?`, `location?`, `necessity`
  (must/should/nice) and context/preconditions via `generic` tags with
  a namespace convention (e.g. `ctx:requires-computer`,
  `place:hardware`).
- A project can be personal (a non-billable project): the same
  taxonomy (ADR-0003), no parallel entity.
- `Budget`: an org-scoped envelope per period (month/quarter/year/
  custom) and category (e.g. home expenses) with an allocatable amount
  and currency; tasks attach via `budget_id`; consumption vs residual
  computed by the service layer.
- Selection within budget is deterministic (priority knapsack), not an
  opaque LLM judgment; the LLM translates the request into a structured
  query and narrates the result. See
  [ADR-0014](adr/0014-personal-domain-budgets.md).

## FR-15 Metering, credits, billing (ADR-0019)

Credit-based billing (reuse the bitvision pattern). Per-org wallet +
append-only idempotent ledger + atomic check-and-debit (no overdraft
under concurrency). Per-model rate card (credits/token in-out,
provider, markup, is_active, tier). Cost bases: local = rate card; our
key = provider cost x markup; BYOK = no token cost, a configurable
platform fee (e.g. 0.0001 x unit), the user key encrypted (ADR-0006).
Metering: a `usage_record` per operation + a debit on the ledger.
Metered storage: DB and S3 at distinct configurable rates (GB-month);
heavy attachments/documents on S3, the DB keeps only metadata +
indexable text. Enforcement in the service layer (choke point like
RBAC): at insufficient credits the cost-incurring operations (LLM,
embedding, advisory with an LLM, heavy storage writes) are rejected
with an i18n code; read, GDPR export and retrieval of
fiscal/conserved documents stay available. Admin (role) tops up
credits, edits rate cards/percentages/storage rates; audited actions. A
payment gateway is out of v1 (v1 = manual admin grant). See
[ADR-0019](adr/0019-metering-credits-billing.md).

## FR-16 Voice notes and conversational capture (ADR-0020)

Capture separated from processing. Offline-first capture (PWA:
MediaRecorder, IndexedDB queue, Service Worker background sync,
resumable presigned multipart upload to S3); raw audio on S3, never
the DB; capture is NOT a cost operation (works offline and at zero
credits, so you do not lose the idea while running). `Note` entity
(kind voice|text|conversation): transcript, S3 audio_ref, optional LLM
outputs (title/summary/action item -> Task reusing the email->task
flow), tags, (org, project) scope, provenance for GDPR erasure.
Pluggable `TranscriptionProvider` (ADR-0012 pattern): default local
multilingual STT (Whisper/faster-whisper, CPU/ARM, replaceable with
GPU/large/API); external STT only with per-Org opt-in + audit (audio
is personal data). Conversation/brainstorming mode (text or voice):
user/LLM turns (provider ADR-0012, metered ADR-0019), saved as Notes
and summarized into memory (ADR-0016) within the (org, project)
isolation. Metering: STT per audio minute, TTS per characters/seconds;
the rate-card unit is first-class
(token|audio-min|tts-char|GB-month), refines ADR-0019. Configurable
audio retention, default delete after confirmed transcription (GDPR
minimization); cascading erasure (S3 audio + transcript + memory blob
+ generated tasks). Honest constraint: background capture on the web
is limited (v1 foreground + queue; always-on = native app, mobile
later). Interaction model: the LLM replies. Online: a live loop
(you speak -> STT -> LLM -> text reply; spoken only if TTS is
enabled). Offline (typical while running with no signal): STT/LLM are
server-side, metered, online; the question is captured offline (not
metered, never lost) and answered as soon as connectivity returns
(the worker runs the LLM, queues the answer turn into the Note and
notifies via FR-12). On-device LLM for true offline replies = out of
scope. Outbound TTS/voice: in v1 via a pluggable `TtsProvider` (local
default, external opt-in), metered per characters/seconds (ADR-0019);
online; offline the reply is text notified and spoken on reconnect.
See
[ADR-0020](adr/0020-voice-notes-conversational-capture.md).
Natural-language commands ("create a note", "new conversation
[in project X]"): a deterministic intent layer for the canonical
commands + an LLM fallback, project resolution with confirmation
(never mis-scoping), voice/text/MCP (ADR-0021). Hands-free activation
from the headphone button or OS assistant: requires a native companion
app, post web-v1 (ADR-0022).
