# Decisions

Product and architecture decisions, consolidated and locked. The *why*
and the rejected alternatives are in the [ADRs](adr/README.md).

## Decisions table

| # | Topic | Decision |
|---|---|---|
| 1/A | SDI invoicing | v1 B2B/B2C only (PA deferred). Single shared channel; Mycelium as transmitter under a per-issuer-profile transmission mandate, never as soggetto emittente (ADR-0053); tenant identity in the FatturaPA payload; conservation = free AdE service (per-tenant adhesion); introduced in phases |
| 2/B | Email auth | Gmail OAuth2; Proton via Bridge sidecar; generic IMAP/SMTP |
| 3 | Scheduling | Deterministic logical CPM + per-person serialization of non-delegated human tasks around appointments; not generic RCPSP |
| 4 | Workflow states | Configurable per Org, project override |
| 5 | Dependencies | 4 types (FS/SS/FF/SF) + lag/lead in calendar time |
| 6 | Concurrency | Optimistic concurrency via `version` (conflict = 409) + append-only activity log; no last-write-wins |
| 7 | Notifications | Telegram + email; additional channels prepared |
| 8 | Migration | None |
| 9 | Memory | Hierarchical on pgvector; hard isolation per (org, project) |
| 10 | Mobile | Responsive web now; API-first for future mobile |
| 11 | Hosting | Cloud, PostgreSQL, ARM node; K8s-ready design |
| 12 | Name | "Mycelium" |
| 13 | Tag | A single `tag` concept with `kind`; client/project with typed satellite profiles (FK to `tag.id`), not free JSONB |
| C | Embedding | Local, pluggable `Embedder` abstraction, re-embedding job |
| D | Retrieval | Hybrid lexical + semantic baseline, RRF fusion (k ~ 60), (org, project) scope |
| E | LLM/Embedding abstraction | Reuse the bitvision_phoenix pattern; Mycelium adds `EmbedderProvider` |
| F | Memory isolation | Hard boundary per (org, project): mandatory RLS + partition + predicate; never relevance only |
| G | No-ubiquity | Appointments unified onto `tasks` via `start_at` + `duration_minutes` (ADR-0008 addendum, migration 0094); per-identity overlap is rejected via the GiST EXCLUDE on `task_participants` (enforced for the assignee + every explicit participant, mig 0095/0096) |
| H | Executor | `task.executor` = human user (serial) or LLM agent (parallel, off the human timeline) |
| I | Planning assistant | Advisory layer in the v1 core on top of the scheduler: deterministic core (feasibility + ranking + constrained selection), LLM/MCP as the natural-language frontend |
| J | Personal domain + budget | Modeled in v1: tasks with cost/location/context/necessity; budget envelope per period/category; deterministic selection within budget (priority knapsack) |
| K | Advanced memory | Tiering by frequency/recency/importance (cold always retrievable, rare != not important); corrective grader with no web branch; retrieval as an agentic MCP tool; structural, non-textual graph; multimodal deferred (ADR-0016) |
| L | Archive backup target | Double copy DB + external store via a pluggable `ArchiveBackupTarget`, async/idempotent, separate from legal conservation (ADR-0010). v1 = S3 EU object storage; Proton Drive backend experimental via an rclone sidecar, then the official Proton SDK (ADR-0018) |
| M | Metering and credits | Credit-based billing: per-org wallet + append-only idempotent ledger + atomic check-and-debit; per-model rate card (reuse the bitvision pattern); local/our-key/BYOK with distinct cost bases (BYOK = configurable platform fee); DB and S3 storage at distinct rates; admin tops up credits; at zero credits stop cost-incurring operations, access/export/legal data preserved (ADR-0019) |
| N | Voice notes and capture | `Note` (voice/text/conversation); offline-first PWA capture -> S3, not metered (even at zero credits/offline); pluggable STT local-default (external opt-in+audit); the LLM replies (online live, offline deferred + notification FR-12); TTS voice-out in v1 (pluggable `TtsProvider`, metered); brainstorming saved as a note and into memory; metering with a generalized unit (refines ADR-0019); configurable audio retention; phase F6b (ADR-0020) |
| O | Command/intent layer | NL -> deterministic action: a deterministic/offline/unmetered canonical grammar + a metered LLM fallback; project resolution with confirmation (never mis-scoping, ADR-0007); explicit default scope; voice/text/MCP (ADR-0021) |
| P | Hands-free activation | A headphone button/OS assistant requires a native component; the web PWA cannot do it with the screen off; hands-free = post web-v1 via a native companion app (decision #10, ADR-0022) |

## Resolved scope decisions

1. SDI v1 = B2B/B2C only. PA/B2G, signature, NE/DT/EC/SE notifications
   and the passive cycle are deferred post-v1.
2. Conservation = free AdE service with per-tenant adhesion. Mycelium
   tracks and guides the adhesion; effective coverage from when
   invoices transit SdI; invoices from the initial manual export are
   out of coverage, the tenant's responsibility.
3. Layered MVP accepted: everything else complete from the start, SDI
   at a minimal fiscal profile and extended in phases.
4. The advisory planning assistant = v1 core (deterministic core,
   LLM/MCP frontend); personal domain and budget modeled in v1.

Decided on the user's explicit direction: hard per-(org, project)
memory isolation; no-ubiquity; serial human executor vs parallel LLM.
