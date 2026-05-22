# Phased roadmap and verification criteria

Every domain phase exposes REST + MCP tools from the start (MCP is not
a final standalone phase). The repo starts empty: the criteria are
end-to-end per phase.

## Phases

- **F0 Foundations**: monorepo scaffold, CI, Docker Compose arm64,
  Postgres+pgvector with **mandatory RLS and per-org memory
  partitioning**, Alembic, auth/JWT, Org + fiscal profile / User /
  Membership / RBAC, **optimistic concurrency** + append-only activity
  log, skeleton `api`/`mcp`/`web`/`worker`/`sdi-inbound`.
- **F1 Tasks + taxonomy**: Task/subtasks/comments, tags with kind +
  client/project satellite profiles, `executor`, list/board views,
  search/filters.
- **F2 Workflow + dependencies + graph**: WorkflowDefinition + project
  override, 4 dependency types + lag, cycle detection, DAG graph.
- **F3 Deterministic scheduler**: logical CPM + per-person
  serialization + actuals + pins; Events + no-ubiquity; Gantt with the
  logical critical path and drag.
- **F4 Time tracking**: realtime timer (a single active timer per
  user, guaranteed by a partial unique index), manual entries, reports
  per project/client/generic/user/task with billable totals and a rate
  snapshot, CSV export; feeds F3 via `actual_start`. PDF export = a
  thin presentation-only follow-up (CSV satisfies the data-export
  requirement), not a blocker.
- **F4b Personal domain, budget, advisory assistant**: task attributes
  (cost, location, context, necessity), `budgets` envelope, advisory
  capabilities (what-can-i-do-now / errand bundling / prioritization
  within budget) in the service layer exposed via REST + MCP;
  deterministic core, LLM/MCP frontend.
- **F5 Email**: Gmail OAuth2 + generic IMAP connector, sync, triage,
  email-to-task, SMTP send (Proton Bridge to follow).
- **F5b Billing & metering core**: wallet + credit_ledger
  (append-only, idempotent, atomic check-and-debit) + model rate cards
  + storage rates (DB vs S3) + enforcement in the service layer +
  admin grant/rate (ADR-0019). Precedes the cost-incurring phases (F6);
  metering hooks added with each metered subsystem.
- **F6 Memory**: tiering by frequency/recency/importance (ADR-0016,
  invariant: cold stays always retrievable), summarization, pluggable
  `Embedder` + pgvector, hybrid RRF retrieval within (org, project), a
  **corrective grader** (no web branch), retrieval exposed as an
  agentic MCP tool, consolidation with provenance, a re-embedding job,
  GDPR erasure. Multimodal and textual GraphRAG explicitly out
  (deferred).
- **F6b Voice notes and conversational capture**: `Note` entity,
  offline-first PWA capture + S3 upload (not metered), worker STT
  pipeline (local `TranscriptionProvider`, ADR-0012/0019) ->
  transcript, optional LLM (title/summary/action item -> Task),
  embedding into memory (ADR-0016); text/voice conversation with an
  LLM reply (online live, offline deferred + notification FR-12);
  ADR-0020; deterministic canonical NL commands + an LLM fallback
  (ADR-0021); TTS voice-out in v1 (`TtsProvider`, metered). After
  F5b/F6.
- **F7a B2B/B2C invoices**: FatturaPA XML + validation,
  `ManualExportChannel`, immutability, concurrency-safe numbering,
  search, mark paid, TD04 credit note; AdE conservation-adhesion
  tracking + marking out-of-coverage invoices.
- **F7b SdICoop test**: `SdICoopChannel` on the test environment +
  inbound SOAP endpoint + receipt parsing (RC/MC/NS/AT) +
  interoperability tests; from here AdE conservation is effective.
- **F7c SdICoop production**: service agreement + accreditation +
  switching the channel to production (a heavy item, resourced as
  such). Implementation status (2026-05-22): F7a complete. F7b is now
  implemented as code, config-gated on `FLOW_SDI_CHANNEL=sdicoop`:
  official FatturaPA XSD validation at transmit; the per-issuer
  `SdiMandate` + `TerzoIntermediarioOSoggettoEmittente` /
  `SoggettoEmittente=TZ` intermediary payload; a real SdICoop `RiceviFile`
  SOAP client over mutual TLS with a per-intermediary file name /
  ProgressivoInvio sequence; and the inbound `/sdi/notifica` receiver that
  correlates RC/MC/NS/AT to the tenant by `IdentificativoSdI` (a SECURITY
  DEFINER cross-org resolver, the 0068 owner-bypass pattern). The SOAP
  envelope build + notification parse are unit-tested; the network calls are
  never exercised in CI. What remains for F7c is external, not code: the AdE
  service agreement + accreditation, the channel certificates, and verifying
  against the AdE test environment the exact WSDL namespace/operation, the
  SOAP esito response, and whether WS-Security signing is required for the
  profile. Inbound mutual TLS is terminated at the edge.
- **Post-v1**: PA/B2G (CAdES/XAdES signature + qualified certificate,
  NE/DT/EC/SE), passive cycle, reverse charge/self-billing TD16-TD19,
  foreign clients, quarterly stamp-duty settlement, CP-SAT optimizing
  leveling, Proton Drive `ArchiveBackupTarget` (rclone sidecar, then
  the official Proton SDK; ADR-0018), a native companion app for
  hands-free (headphone button / OS assistant) and always-on capture
  (ADR-0022).
- **F8 Notifications + recurrences + finishing**: Telegram + email,
  reminders, recurring tasks, security/privacy hardening, audit,
  `ArchiveBackupTarget` with an S3 EU object-storage backend (double
  copy DB + external, async/idempotent; ADR-0018; distinct from AdE
  legal conservation, ADR-0010).

## End-to-end verification criteria

- **F0**: `docker compose up` (arm64) starts everything; signup/login;
  Org A does not see Org B's data; project P1 does not see P2's memory
  (RLS + partition + predicate); a concurrent stale write -> 409.
- **F1**: the same task created from GUI/REST/MCP -> identical state;
  a client+project tag on a task and consistent filtering.
- **F2**: an edge that creates a cycle is rejected; a project workflow
  override imposes the extra state; the graph renders the DAG.
- **F3**: two human tasks with the same assignee and no dependency do
  not overlap; an LLM-delegated task can be parallel; an overlapping
  appointment for the same person is rejected; SS with +2 working-day
  lag across a holiday -> exact expected dates; an in-progress task
  with 12h logged vs a 4h estimate -> the residual rule + pinned ES;
  drag that survives a recompute for an unrelated change; same input
  -> identical schedule.
- **F4**: a timer started from MCP visible in the GUI in realtime;
  reports per client/project with the expected totals; an openable
  export.
- **F4b**: given a free window (duration + location) the assistant
  proposes only feasible tasks not in no-ubiquity conflict, with a
  deterministic, explainable ranking; "what do I need at the hardware
  store" aggregates items by location/context within the user's org;
  given a budget the within-envelope selection is knapsack-correct and
  explainable (must-have first); same input -> same result.
- **F5**: a Gmail account (OAuth2) configured; email-to-task with
  tag/client/project and a source link; an SMTP reply delivered.
- **F5b**: at zero credits an LLM/embedding operation is rejected with
  an i18n code; read/export/fiscal data stay accessible; idempotent
  debits (a retry does not double-charge); no overdraft under
  concurrency; an audited admin grant.
- **F6**: hybrid search (RRF) within (org, project) retrieves an old
  thread demoted to cold; a query with a rare token found by the
  lexical branch; an unfiltered search does not lose cross-project/org
  data; deleting a message propagates to embedding/summary/
  object-storage/consolidated blobs; a model change -> re-embedding
  with no write downtime.
- **F6b**: a voice note recorded offline and queued at zero credits
  (capture not blocked); on sync local STT produces the transcript
  that enters memory within (org, project); a question asked offline
  -> a deferred LLM answer appended to the Note + notification; erasing
  a note cascades to S3 audio + transcript + memory blob + tasks;
  online the LLM reply is also spoken (TTS).
- **F7a**: a B2B/B2C invoice from a billable time entry, schema-valid
  XML, a downloadable manual export; immutable after emission;
  concurrent numbering with no duplicates/gaps; a linked TD04; the AdE
  conservation-adhesion status tracked and out-of-coverage invoices
  marked.
- **F7b**: a push SdI notification on the inbound endpoint, correlated
  by `IdentificativoSdI`; RC/MC/NS/AT parsed; AdE conservation
  effective.
- **F7c**: an accredited production channel; a real invoice delivered
  (RC).
- **F8**: reminders and SDI outcomes delivered on Telegram and email;
  a recurring task materialized by the worker.
- **Automated tests**: per [non-functional requirements, Testing
  section](non-functional-requirements.md).
