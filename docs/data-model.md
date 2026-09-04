# Data model

Logical schema. Every org-scoped entity has `org_id`, `version`
(optimistic concurrency) and timestamps. Mandatory RLS on every
org-scoped entity. Memory is partitioned by `org_id`.

## Tenancy and taxonomy

- `organizations` (the tenant table; user-facing name "workspace",
  ADR-0024) `(id, name, fiscal_profile: { regime RF.., p_iva, cf,
  structured address, rea, cassa })`
- `users(id, email, password_hash, ...)` (global)
- `memberships(org_id, user_id, role[owner|admin|member|guest])`
- `tags(id, org_id, kind[generic|client|project|memory_channel], name,
  color, status, version)` — `client` and `project` are the
  **structural** kinds (cardinality-constrained on the junctions,
  ADR-0050); `generic` and `memory_channel` are free-form facets,
  unconstrained many-to-many
- `client_profile(tag_id PK FK->tags.id, ragione_sociale,
  id_fiscale_iva(country,id), codice_fiscale, address(indirizzo,cap,
  comune,provincia,nazione), codice_destinatario|pec)`
- `project_profile(tag_id PK FK->tags.id, client_tag_id FK->tags.id
  NOT NULL, rate, currency, budget, workflow_id?)` — every project has
  exactly one client (ADR-0050 (d)), and it is the truth from which a
  tagged entity's client is derived
- `task_tags(task_id, tag_id)` (one relation per kind: exactly one
  `client` row and exactly one `project` row per task, the client
  being that project's `client_tag_id`)

## Tasks, dependencies, scheduling

- `tasks(id, org_id, title, description, state_id, priority,
  start_date?, due_date?, estimate_effort_h?, remaining_effort_h,
  actual_start?, is_milestone, executor_kind[human|llm_agent],
  executor_user_id?, schedule_mode[auto|manual],
  constraint_kind[none|SNET|MSO|MFO], constraint_date?,
  monetary_cost?, location?, necessity[must|should|nice], budget_id?,
  index_scope[org|none], parent_task_id?, created_by, version)`
  - `index_scope='none'` keeps title and description out of the
    automatic search index. It is not a read boundary and not a filter
    on the free-text `q=`; see ADR-0056.
  - context/preconditions via `generic` tags with a namespace
    convention (e.g. `ctx:requires-computer`, `place:hardware`)
- `task_assignees(task_id, user_id)`
- `task_dependencies(id, org_id, predecessor_id, successor_id,
  type[FS|SS|FF|SF], lag_working_minutes signed)`
  - constraints: no self-dependency, unique(predecessor_id,
    successor_id, type), predecessor and successor same `org_id`
- `schedule(task_id PK, es, ef, ls, lf, slack,
  on_logical_critical_path, scheduled_start, scheduled_end,
  computed_at, input_fingerprint)` (derived; not subject to user
  optimistic concurrency; the most recent recompute supersedes the
  previous)

## Calendars and appointments

- `working_calendars(id, org_id, name, is_default, weekly_hours,
  timezone)`
- `calendar_holidays(calendar_id, date)`
- `user_calendar(user_id, calendar_id, daily_capacity_h)`
- **Appointments are tasks** (migration 0094, ADR-0008 addendum): a
  task with `start_at` (timestamptz) + `duration_minutes` (int) IS
  the calendar block. The pair is enforced by a CHECK constraint
  (both set or both NULL).
- `task_participants(task_id, identity_id, org_id, start_at,
  duration_minutes)` — additional identities pinned to an
  appointment-task. The window is denormalised so the GiST EXCLUDE
  constraint
  `no_overlap_task_participants(identity_id, tstzrange(...))` enforces
  no-ubiquity per identity. The 0096 trigger
  `sync_task_assignee_participant` mirrors the assignee into a
  participant row, so the single EXCLUDE covers both the assignee
  and every extra invitee. The 0095 trigger
  `sync_task_participants_window` keeps the denormalised columns
  aligned with the parent task and removes all participants if the
  appointment status is dropped (duration_minutes → NULL).
- Google Calendar ingest writes appointment-tasks; the sync state
  (`external_provider`, `external_id`, `external_subscription_id`)
  lives on `tasks` (migration 0097, partial UNIQUE on
  `(external_subscription_id, external_id)`). The legacy `events` /
  `event_participants` tables are gone.

## Workflow

- `workflow_defs(id, org_id, name, is_default)`
- `workflow_states(id, workflow_id, name, ord, is_initial, is_terminal)`
- `workflow_transitions(id, workflow_id, from_state_id, to_state_id)`
- project override via `project_profile.workflow_id`

## Time tracking

- `time_entries(id, org_id, task_id, user_id, started_at, ended_at?,
  duration?, billable, rate_snapshot, description, version)`

## Personal domain and budget

- `budgets(id, org_id, name, category, period[month|quarter|year|
  custom], period_start, period_end, amount, currency, version)`
- `tasks.budget_id? FK -> budgets.id`; `tasks.monetary_cost?`;
  `tasks.location?`; `tasks.necessity[must|should|nice]`
- Advisory capabilities (what-can-i-do-now, errand bundling,
  prioritization within budget) live in the service layer, not in
  tables: deterministic queries over
  `tasks` (incl. appointment-tasks) / `schedule` /
  `task_participants` / `budgets` accessible to the user within an
  org.

## Metering and credits

- `wallets(org_id PK, balance_credits, version)`
- `credit_ledger(id, org_id, kind[grant|debit], credits, operation_id,
  ref, created_at)` append-only (trigger); idempotent on
  (org_id, operation_id)
- `model_rate_cards(model_id PK, provider, credits_in_per_ktok,
  credits_out_per_ktok, provider_cost_basis?, markup_pct, tier,
  is_active)`
- `storage_rates(scope[db|s3] PK, credits_per_gb_month)`
- `usage_records(id, org_id, op, model_id?, tokens_in?, tokens_out?,
  bytes?, period?, credits, created_at)`
- `byok_accounts(id, org_id, user_id, provider, secret_ref,
  platform_fee_factor)` (encrypted key, ADR-0006)
- Enforcement in the service layer (choke point, like RBAC); admin
  grant via an audited privileged path. See ADR-0019.

## Notes and voice capture

- `notes(id, org_id, kind[voice|text|conversation], title?, summary?,
  audio_ref?, status, index_scope[org|none], created_by, version)` (the
  body lives in `note_part`; migration 0012 dropped `notes.transcript`)
  - `index_scope` is on the note, not on the part, so a part added to a
    scoped-out note is born scoped out too. ADR-0056.
- `note_tags(note_id, tag_id)` (one relation per kind, like
  `task_tags`; migration 0016 dropped `notes.project_id`, so the
  junction is the only truth). A note carries exactly one `client` row
  and AT MOST one `project` row: no project = the personal retrieval
  perimeter (`memory_blobs.project_id` NULL), a first-class state and
  not a missing field. ADR-0050.
- `note_turns(note_id, ord, role[user|llm], content)` (conversation
  kind)
- `note_tasks(note_id, task_id)` (action item -> Task, the email->task
  flow)
- raw audio on S3 (`audio_ref`), never in the DB; configurable
  retention (default: delete after confirmed transcription)
- rate card / `usage_records`: first-class unit
  (token|audio_minutes|tts_chars|gb_month), refines ADR-0019
- (org, project) isolation + provenance for cascading GDPR erasure
  (S3 audio + transcript + memory blob + generated tasks). See
  ADR-0020.

## Email

- `email_accounts(id, org_id, user_id, connector[gmail_oauth|
  proton_bridge|generic_imap], imap_*, smtp_*, auth_type, secret_ref,
  bridge_endpoint?, status, version)`
- `email_messages(id, org_id, account_id, message_id, thread_id,
  "from", subject, snippet, body_ref, received_at,
  thread_last_activity, linked_task_id?)`

## Memory

- `memory_blobs(id, org_id, project_id?, namespace, tier[hot|warm|
  cold], text?, summary?, embedding vector, model_id, dim,
  fts tsvector, trgm, last_accessed_at, access_count, access_score,
  importance, concept_id?)` (access_score = frequency + recency with
  decay; tiering driven by score+importance, ADR-0016; cold stays
  always queryable)
  - `PARTITION BY org_id`; mandatory RLS; mandatory
    (org_id, project_id) predicate in every query. `project_id` NULL is
    the personal perimeter, not "unscoped": it is where a projectless
    note indexes, and it is queried explicitly (ADR-0050)
  - `memory_blob_tags` is deliberately NOT cardinality-constrained:
    consolidation unions the member blobs' tags as provenance, and the
    authoritative perimeter is the scalar `project_id` above (ADR-0050)
  - per-partition indexes: HNSW on `embedding`, GIN on `fts`, GIN/GiST
    trigram on `trgm`
- `blob_sources(blob_id, source_kind, source_id, created_at)`
  (explicit N:1 provenance for GDPR deletion)

## Invoicing and conservation

- `invoices(id, org_id, client_tag_id, kind[invoice|credit_note],
  parent_invoice_id?, number, series, year, state[draft|transmitted|
  terminal], identificativo_sdi?, sdi_status, payment_status, paid_at?,
  paid_amount?, xml_ref, version)`
  - immutable after emission; the number is allocated concurrency-safe
    only at the draft -> transmitted transition
- `invoice_lines(invoice_id, ..., natura?, esigibilita_iva?, ritenuta?,
  cassa?, bollo?)`
  - `quantity` / `unit_price` are `Numeric(_,4)` and the XML emits them
    at those same 4 decimals, so `PrezzoTotale` equals the product of
    the operands the receiver reads (SdI re-checks that arithmetic)
- `invoice_line_altri_dati(invoice_line_id, ord, tipo_dato,
  riferimento_testo?, riferimento_numero?, riferimento_data?)`
  - FatturaPA `AltriDatiGestionali` (2.2.1.16), 0..N per line and
    ORDERED (`ord`, unique per line): a typed child table rather than
    JSONB on the line, per ADR-0003. Empty by default; editable only
    while the invoice is a draft (ADR-0009). Migration 0088.
- `sdi_mandates(id, org_id, scope, valid_from, valid_to, revoked_at,
  audit)`
- `conservation_records(invoice_id|receipt_id, provider[ade_free],
  adhesion_status, in_coverage bool, hash?, conserved_at?)`
- `invoices` also carries `progressivo_invio` / `nome_file` (the emitted SdI
  file identity, COMMITTED before dispatch and reused verbatim on a resend;
  partial index `ix_invoices_nome_file` — it is the lost-ACK correlation key),
  `sdi_dispatch_started_at` (the two-phase dispatch lease: non-null = an
  unsettled dispatch owns the invoice; expired + null `identificativo_sdi` =
  retryable) and `sdi_env_used` (the SdI environment stamped at the
  pre-dispatch commit; a retry under a flipped environment is refused).
  ADR-0045/ADR-0046.
- `issuer_api_keys(id, org_id, issuer_profile_id FK cascade, created_by FK
  users set-null, name, key_public_id UNIQUE, secret_hash UNIQUE (=HMAC-SHA256
  (pepper, raw)), permissions text[], previous_secret_hash?, rotated_at?,
  previous_secret_expires_at?, previous_secret_last_used_at?, expires_at NOT
  NULL, last_used_at?, revoked_at?, version)` -- a per-issuer credential for the
  public Invoice API (`/api/v1`); ENABLE RLS so the SECURITY DEFINER
  `authenticate_issuer_api_key` reads a row with no tenant GUC (ADR-0045).
- `api_idempotency(id, org_id, issuer_profile_id, endpoint, idempotency_key,
  request_hash, response_snapshot jsonb?, invoice_id?, created_at)` -- UNIQUE
  `(issuer_profile_id, endpoint, idempotency_key)`, FORCE RLS; the atomic claim
  that stops a retry double-filing an invoice.
- `issuer_key_rate_limit(key_id, endpoint_class, org_id, window_start, count)` --
  the per-key fixed-window rate bucket (FORCE RLS).

## Inbound payment connectors (ADR-0051, migrations 0092-0098)

- `payment_connectors(id, org_id, issuer_profile_id FK cascade, created_by FK
  users set-null, provider[stripe|mycelium], label, signing_secret_ciphertext,
  previous_signing_secret_ciphertext?, previous_signing_secret_expires_at?,
  signing_secret_ciphertext NULLABLE (a vendor connector exists before its
  provider has issued one -- the URL carries its id, so it must exist first;
  ENABLING is what requires a secret), api_key_hash?, previous_api_key_hash?,
  previous_api_key_expires_at? (armed only for providers that can send a
  custom header, i.e. the native contract), enabled,
  invoice_mode[transmit|draft|dry_run|off],
  credit_note_mode[transmit|draft|dry_run|off],
  emission_event[invoice.paid|payment_intent.succeeded|
  checkout.session.completed], refund_event[refund.created|charge.refunded],
  payment_sync_enabled, series?,
  default_purpose?, default_vat_rate?, default_vat_nature?,
  default_line_description?, amounts_include_vat,
  default_payment_conditions_code?, default_payment_method_code?,
  default_country_code?, vat_pricing[auto|gross|net], metadata_vat_keys text[], metadata_tax_code_keys
  text[], metadata_sdi_keys text[], metadata_pec_keys text[], revoked_at?,
  last_event_at?, version)` -- UNIQUE `(issuer_profile_id, label)`;
  `emission_event` and `refund_event` are each ONE value rather than a set,
  because a provider announces the same money several times in both directions
  and honouring every announcement would double-invoice (emission) or file two
  credit notes for one refund (reversal: the two Stripe announcements only
  deduplicate while the charge carries expanded `refunds.data`). The signing
  secret is a reversible Fernet envelope, the optional ingress key a peppered
  one-way hash. ENABLE (not FORCE) RLS, so the SECURITY DEFINER
  `resolve_payment_connector(uuid)` reads a row with no tenant GUC; it returns
  nothing for a revoked row and NULLs an expired grace copy.
- `payment_connector_refusals(connector_id PK FK cascade, org_id FK cascade,
  window_start, count)` -- fixed-window cap on how many REFUSED deliveries per
  connector get appended to the ledger, so an unauthenticated caller who
  learned a URL cannot grow it without bound. Counts refusals, never requests:
  a signed burst is by definition the provider. ENABLE (not FORCE) RLS, like
  `payment_connectors`, because it is maintained on the no-tenant ingress path.
- `payment_connector_events(id, org_id, connector_id FK cascade,
  provider_event_id, event_type, payload jsonb, occurred_at?,
  status[pending|processing|done|ignored|no_billing_data|needs_attention|dead],
  attempt_count, max_attempts, next_attempt_at, last_attempt_at?, processed_at?,
  provider_customer_id?, dry_run, dry_run_xml?, last_error?, error_detail?,
  invoice_id? FK invoices
  set-null)` -- the ingress ledger and the work queue. UNIQUE
  `(connector_id, provider_event_id)`; index on `(connector_id, created_at)`
  plus partial indexes on `next_attempt_at WHERE status='pending'`, on
  `last_attempt_at WHERE status='processing'`, on `(connector_id, created_at)
  WHERE status IN ('needs_attention','dead')` and on `(connector_id,
  provider_customer_id) WHERE status='no_billing_data'` (the re-arm sweep).
  FORCE RLS.
- `payment_object_links(id, org_id, connector_id FK cascade, object_kind[invoice|
  payment_intent|checkout_session|charge|credit_note|refund], object_id,
  invoice_id FK invoices RESTRICT)` -- UNIQUE `(connector_id, object_kind,
  object_id)`, index on `invoice_id`. FORCE RLS.
- `payment_webhook_deliveries(id, org_id, connector_id FK cascade, provider,
  outcome[accepted|duplicate|signature_invalid|disabled|payload_invalid|
  too_large], http_status, event_id? FK payment_connector_events set-null,
  provider_event_id?, body_bytes, body_sha256?, signature_present,
  api_key_present, received_at)` -- one row per inbound delivery attempt; the
  body is represented by its SHA-256, never stored. Index on
  `(connector_id, received_at)` plus a partial one `WHERE outcome NOT IN
  ('accepted','duplicate')`. FORCE RLS.
- `payment_customer_links(id, org_id, connector_id FK cascade,
  provider_customer_id, client_tag_id)` -- UNIQUE `(connector_id,
  provider_customer_id)`, index on `client_tag_id`. Like
  `invoices.client_tag_id` it is a bare UUID with no FK. Holds NO fiscal data:
  the counterpart's fiscal identity lives in `client_profile`, fed from the
  provider's customer events through `taxonomy.fill_client_gaps`. FORCE RLS.
- Migration 0092 also widens the `activity_log` and `entity_revision`
  actor-kind CHECKs with `payment_connector`.

## Notifications, collaboration, audit

- `notifications(id, org_id, user_id, channel, event_type, payload,
  status)`
- `notification_prefs(user_id, event_type, channels)`
- `comments(id, org_id, task_id, user_id, body, created_at)`
- `attachments(id, org_id, task_id?, email_message_id?, blob_ref)`
- `activity_log(id, org_id, actor_id, entity, entity_id, action, diff,
  ts)` (append-only)
