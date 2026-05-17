# Modello dati

Schema logico. Tutte le entita org-scoped hanno `org_id`, `version`
(optimistic concurrency) e timestamp. RLS obbligatoria su tutte le
entita org-scoped. La memoria e partizionata per `org_id`.

## Tenancy e tassonomia

- `organizations(id, name, fiscal_profile: { regime RF.., p_iva, cf,
  sede strutturata, rea, cassa })`
- `users(id, email, password_hash, ...)` (globale)
- `memberships(org_id, user_id, role[owner|admin|member|guest])`
- `tags(id, org_id, kind[generic|client|project], name, color, status,
  version)`
- `client_profile(tag_id PK FK->tags.id, ragione_sociale,
  id_fiscale_iva(paese,id), codice_fiscale, sede(indirizzo,cap,comune,
  provincia,nazione), codice_destinatario|pec)`
- `project_profile(tag_id PK FK->tags.id, client_tag_id FK->tags.id,
  tariffa, valuta, budget, workflow_id?)`
- `task_tags(task_id, tag_id)` (relazione unica per ogni kind)

## Task, dipendenze, scheduling

- `tasks(id, org_id, title, description, state_id, priority,
  start_date?, due_date?, estimate_effort_h?, remaining_effort_h,
  actual_start?, is_milestone, executor_kind[human|llm_agent],
  executor_user_id?, schedule_mode[auto|manual],
  constraint_kind[none|SNET|MSO|MFO], constraint_date?,
  monetary_cost?, location?, necessity[must|should|nice], budget_id?,
  parent_task_id?, created_by, version)`
  - contesto/precondizioni via tag `generic` con convenzione di
    namespace (es. `ctx:richiede-computer`, `place:brico`)
- `task_assignees(task_id, user_id)`
- `task_dependencies(id, org_id, predecessor_id, successor_id,
  type[FS|SS|FF|SF], lag_working_minutes signed)`
  - vincoli: no self-dependency, unique(predecessor_id, successor_id,
    type), predecessore e successore stesso `org_id`
- `schedule(task_id PK, es, ef, ls, lf, slack,
  on_logical_critical_path, scheduled_start, scheduled_end,
  computed_at, input_fingerprint)` (derivata; non soggetta a optimistic
  concurrency utente; la ricomputazione piu recente supera la
  precedente)

## Calendari ed eventi

- `working_calendars(id, org_id, name, is_default, weekly_hours,
  timezone)`
- `calendar_holidays(calendar_id, date)`
- `user_calendar(user_id, calendar_id, daily_capacity_h)`
- `events(id, org_id, project_tag_id?, client_tag_id?, title, start,
  end, location, version)`
- `event_participants(event_id, user_id)`
  - vincolo: nessuna sovrapposizione di intervalli per lo stesso
    `user_id` (no-ubiquita), valido anche verso i task umani schedulati

## Workflow

- `workflow_defs(id, org_id, name, is_default)`
- `workflow_states(id, workflow_id, name, ord, is_initial, is_terminal)`
- `workflow_transitions(id, workflow_id, from_state_id, to_state_id)`
- override progetto via `project_profile.workflow_id`

## Time tracking

- `time_entries(id, org_id, task_id, user_id, started_at, ended_at?,
  duration?, billable, rate_snapshot, description, version)`

## Dominio personale e budget

- `budgets(id, org_id, name, category, period[month|quarter|year|
  custom], period_start, period_end, amount, currency, version)`
- `tasks.budget_id? FK -> budgets.id`; `tasks.monetary_cost?`;
  `tasks.location?`; `tasks.necessity[must|should|nice]`
- Le capacita advisory (cosa-faccio-ora, errand bundling,
  prioritizzazione entro budget) sono nel service layer, non tabelle:
  query deterministiche su `tasks`/`schedule`/`events`/`budgets`
  accessibili all'utente entro una org.

## Email

- `email_accounts(id, org_id, user_id, connector[gmail_oauth|
  proton_bridge|generic_imap], imap_*, smtp_*, auth_type, secret_ref,
  bridge_endpoint?, status, version)`
- `email_messages(id, org_id, account_id, message_id, thread_id,
  "from", subject, snippet, body_ref, received_at,
  thread_last_activity, linked_task_id?)`

## Memoria

- `memory_blobs(id, org_id, project_tag_id, namespace, tier[hot|warm|
  cold], text?, summary?, embedding vector, model_id, dim,
  fts tsvector, trgm, last_accessed_at)`
  - `PARTITION BY org_id`; RLS obbligatoria; predicato
    (org_id, project_tag_id) obbligatorio in ogni query
  - indici per partizione: HNSW su `embedding`, GIN su `fts`, GIN/GiST
    trigram su `trgm`
- `blob_sources(blob_id, source_kind, source_id, created_at)`
  (provenienza N:1 esplicita per la cancellazione GDPR)

## Fatturazione e conservazione

- `invoices(id, org_id, client_tag_id, kind[invoice|credit_note],
  parent_invoice_id?, number, series, year, state[draft|transmitted|
  terminal], identificativo_sdi?, sdi_status, payment_status, paid_at?,
  paid_amount?, xml_ref, version)`
  - immutabile dopo emissione; numero allocato in modo
    concorrenza-safe solo alla transizione draft -> transmitted
- `invoice_lines(invoice_id, ..., natura?, esigibilita_iva?, ritenuta?,
  cassa?, bollo?)`
- `sdi_mandates(id, org_id, scope, valid_from, valid_to, revoked_at,
  audit)`
- `conservation_records(invoice_id|receipt_id, provider[ade_free],
  adhesion_status, in_coverage bool, hash?, conserved_at?)`

## Notifiche, collaborazione, audit

- `notifications(id, org_id, user_id, channel, event_type, payload,
  status)`
- `notification_prefs(user_id, event_type, channels)`
- `comments(id, org_id, task_id, user_id, body, created_at)`
- `attachments(id, org_id, task_id?, email_message_id?, blob_ref)`
- `activity_log(id, org_id, actor_id, entity, entity_id, action, diff,
  ts)` (append-only)
