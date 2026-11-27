# Architecture Decision Records

Each ADR records a non-obvious decision: context, decision,
consequences, rejected alternatives. Several decisions here correct the
naive choice that surfaced in critical review: they must not be
regressed.

Format: Status, Context, Decision, Consequences, Alternatives rejected.

## Index

- [0001 Architecture: Python monorepo, single service layer](0001-architecture-monorepo-python-service-layer.md)
- [0002 Multi-tenant: optimistic concurrency, mandatory RLS](0002-multi-tenant-optimistic-concurrency-rls.md)
- [0003 Unified tag with typed satellite profiles](0003-tag-unified-typed-satellite-profiles.md)
- [0004 Deterministic scheduler, not RCPSP](0004-deterministic-scheduler.md)
- [0005 Hierarchical memory on pgvector, hybrid RRF retrieval](0005-hierarchical-memory-pgvector-hybrid-rrf.md)
- [0006 At-rest encryption at the volume level](0006-at-rest-volume-encryption.md)
- [0007 Hard memory isolation per (org, project)](0007-memory-hard-isolation-org-project.md)
- [0008 No-ubiquity: events entity](0008-no-ubiquity-events.md) — superseded by addendum: appointments unified onto `tasks` (mig 0094 / 0095 / 0096 / 0097); the legacy events table is gone
- [0009 Invoice immutability, soft-delete carve-out](0009-invoice-immutability.md)
- [0010 Conservation: free AdE service](0010-conservation-ade-free-service.md)
- [0011 SDI: intermediary/mandate model, v1 B2B/B2C](0011-sdi-intermediary-mandate-v1-b2b.md) — revised by [0053](0053-transmitter-not-emitter.md): the mandate is to TRANSMIT, so `TerzoIntermediarioOSoggettoEmittente` / `SoggettoEmittente=TZ` are no longer emitted; `IdTrasmittente` stands
- [0012 LLM/Embedder abstraction, reuse the bitvision pattern](0012-llm-embedder-abstraction.md) — the embedder half is revised by [0061](0061-the-embedder-seam-has-a-side.md): the seam carries a query/document side
- [0013 Planning advisory layer, deterministic core](0013-planning-advisory-layer.md)
- [0014 Personal domain and budget envelope](0014-personal-domain-budgets.md)
- [0015 RLS: two Postgres roles and SECURITY DEFINER provisioning](0015-rls-two-role-and-provisioning.md)
- [0016 Memory: human-like tiering and RAG-pattern alignment](0016-memory-human-tiering-and-rag-alignment.md)
- [0017 English-only project language; i18n-ready message catalog](0017-english-only-i18n-message-catalog.md)
- [0018 Archive backup target (dual copy), distinct from legal conservation](0018-archive-backup-target.md)
- [0019 Metering, credits wallet, rate cards, enforcement](0019-metering-credits-billing.md)
- [0020 Voice notes and conversational capture](0020-voice-notes-conversational-capture.md)
- [0021 Command/intent layer (natural language to deterministic actions)](0021-command-intent-layer.md)
- [0022 Hands-free activation (headphone button): native/OS-assistant](0022-handsfree-activation-native.md)
- [0023 Email connector abstraction and idempotent sync](0023-email-connector-abstraction.md)
- [0024 "Workspace": user-facing name of the tenant; personal-first](0024-workspace-user-facing-tenant-name.md)
- [0025 Resource-aware scheduling and human/LLM work orchestration](0025-work-orchestration-resource-scheduling.md)
- [0026 Telegram as an in-process LLM assistant channel](0026-telegram-llm-assistant-channel.md)
- [0027 Adjudication framework for multi-agent convergence](0027-adjudication-framework.md)
- [0028 Identity-first addressing and explicit task ownership](0028-identity-first-addressing.md)
- [0029 Note garden ecosystem and typed note/task relations](0029-note-garden-ecosystem.md)
- [0030 Embedding model migration (dual-column)](0030-embedding-model-migration.md)
- [0031 Mindmap layout and edge weights](0031-mindmap-layout-edge-weights.md)
- [0032 `garden_classify(node_id)`: the proposal engine (incl. automatic maturity promotion)](0032-garden-classify-api.md)
- [0033 Anti-monoculture safeguards in `garden_classify`](0033-anti-monoculture-suggestions.md)
- [0034 Humus policy in the LLM walk](0034-humus-policy-walk-llm.md)
- [0035 Garden health sensors dashboard](0035-garden-sensors-dashboard.md)
- [0036 Event bus for multi-agent coordination on the graph](0036-agent-event-bus.md)
- [0037 Online learning loop on garden suggestions](0037-online-learning-loop.md)
- [0038 UUID-prefix entity resolver (`/lookup/{prefix}`)](0038-uuid-prefix-resolver.md)
- [0039 Fungal decomposition pipeline (humus producer)](0039-fungal-decomposition-pipeline.md)
- [0040 Mycelial 4-verb note-note link model](0040-mycelial-link-model.md) — revises ADR-0029 D3
- [0041 Autonomous retention spares the originals](0041-autonomous-retention-spares-originals.md)
- [0042 Tasks as graph nodes + complete auto-classify-on-ingest](0042-tasks-as-graph-nodes-and-complete-autoclassify.md)
- [0043 Human-gated review state for AI-generated nodes](0043-human-gated-review-state-for-ai-generated-nodes.md) — superseded in part by [0075](0075-a-proposal-is-read-before-it-is-decided.md): a reject is the stored state `declined`, not a soft-delete
- [0044 Temporal knowledge graph (Track B)](0044-temporal-knowledge-graph.md)
- [0045 Issuer-scoped API keys, public Invoice API, MCP invoice tools](0045-issuer-scoped-api-keys.md)
- [0046 Two-phase durable invoice transmit (lost-ACK safety)](0046-two-phase-durable-transmit.md)
- [0047 Signed outbound webhooks on invoice state changes](0047-signed-invoice-webhooks.md)
- [0048 Fuel-table retention: pruning is hygiene, not metabolism](0048-fuel-table-retention.md)
- [0049 Working memory is delegated to the calling agent](0049-working-memory-delegated-to-the-caller.md)
- [0050 Structural tag cardinality on tasks and notes](0050-structural-tag-cardinality.md) — revises ADR-0003 (silent on cardinality)
- [0051 Inbound payment connectors (provider webhooks -> FatturaPA)](0051-inbound-payment-connectors.md) — revised by [0054](0054-what-may-become-a-causale.md): a provider's free text never becomes a `Causale`
- [0052 Workflow interchange document (JSON, no database identity)](0052-workflow-interchange-document.md)
- [0053 Mycelium is the soggetto trasmittente, never the soggetto emittente](0053-transmitter-not-emitter.md)
- [0054 What may become a Causale, and where the tracciato's charset is enforced](0054-what-may-become-a-causale.md)
- [0055 Two views of one document, not two documents](0055-two-views-of-one-document.md) — revises the single-surface editor decision in `0228012`
- [0056 An indexing class on the row, not a read boundary](0056-index-scope-opt-out-from-automatic-indexing.md)
- [0057 The browser is a fourth surface, and it holds a scoped credential](0057-the-browser-is-a-fourth-surface.md) — decision 5 (the `externally_connectable` handshake) superseded by [0059](0059-the-extension-asks-to-be-let-in.md)
- [0058 Refuse to serve behind the schema, serve ahead of it](0058-refuse-to-serve-behind-the-schema.md)
- [0059 The extension asks to be let in, it is not handed a secret](0059-the-extension-asks-to-be-let-in.md) — supersedes decision 5 of [0057](0057-the-browser-is-a-fourth-surface.md)
- [0060 The fleet embedding dim is schema, and truncation is declared](0060-the-fleet-dim-is-schema-not-configuration.md)
- [0061 The embedder seam has a side, and the prefix comes from the checkpoint](0061-the-embedder-seam-has-a-side.md)
- [0062 The reranker's number has a scale, and the pipeline stops guessing it](0062-the-reranker-number-has-a-scale.md)
- [0063 Possession of a task is an object, not a column](0063-possession-is-not-state.md) — answers the shareable working-set object left open by [0049](0049-working-memory-delegated-to-the-caller.md)
- [0064 An MCP result is charged for the whole session, so the default shape is the cheap one](0064-a-result-is-charged-for-the-whole-session.md) — completes the input half of the short id in [0038](0038-uuid-prefix-resolver.md); amended 2026-11-27: note text is read by windows within a byte budget
- [0065 A live view asks how much changed, not what changed](0065-a-view-asks-how-much-changed-not-what-changed.md) — reads the activity log of [0002](0002-multi-tenant-optimistic-concurrency-rls.md); defers the push transport of [0036](0036-agent-event-bus.md)
- [0066 A person decides a suggestion; an agent may carry the decision out](0066-a-person-decides-an-agent-carries-it-out.md) — reads the append-only activity log of [0002](0002-multi-tenant-optimistic-concurrency-rls.md) as the record of who wrote a decision; amended 2026-11-26: a suggestion a person wrote is its own decision
- [0067 An instruction is a comment assigned to an agent](0067-an-instruction-is-a-comment-assigned-to-an-agent.md) — keeps closing an instruction a person's act, as [0066](0066-a-person-decides-an-agent-carries-it-out.md) does for accepting a suggestion
- [0068 One operation registry, bound by REST and MCP](0068-one-operation-registry-for-rest-and-mcp.md) — accepted; garden migrated, note reads and the paged note list migrated; makes the parity [0064](0064-a-result-is-charged-for-the-whole-session.md) and [0066](0066-a-person-decides-an-agent-carries-it-out.md) assume a property of construction
- [0069 Any MCP host connects through OAuth, and a person consents](0069-any-mcp-host-connects-and-a-person-consents.md) — reuses the request, answer, collect shape of the device grant (migration 0013) for public OAuth clients
- [0070 A period is reported from a collection, not from a summary](0070-a-period-is-reported-from-a-collection-not-a-summary.md) — reads the append-only activity log of [0002](0002-multi-tenant-optimistic-concurrency-rls.md); declared once on the registry of [0068](0068-one-operation-registry-for-rest-and-mcp.md)
- [0071 The graph reads the notes on its own, and a person decides what it believes](0071-the-graph-reads-the-notes-and-a-person-decides.md) — carries out the autonomous extraction deferred by [0044](0044-temporal-knowledge-graph.md) under the review rule of [0043](0043-human-gated-review-state-for-ai-generated-nodes.md); retires `garden_review_gate_enabled`; one point open (whether `kg_extract` is born proposed)
- [0072 A model declares work done only as the checker](0072-a-model-declares-work-done-only-as-the-checker.md) — the close guard reads the possession of [0063](0063-possession-is-not-state.md); one credential predicate replaces four; writing a workflow becomes a person's act and retires `workflows:write`
- [0073 What a person writes in a body is a link, and the graph reads it without guessing](0073-what-a-person-writes-is-a-link.md) — proposed; reopens item #F of [0038](0038-uuid-prefix-resolver.md); leaves the typed links of [0040](0040-mycelial-link-model.md) unchanged; proposals follow the review rule of [0043](0043-human-gated-review-state-for-ai-generated-nodes.md) and [0071](0071-the-graph-reads-the-notes-and-a-person-decides.md)
- [0074 A search can ask a source outside the workspace, and the workspace keeps the last word](0074-a-search-asks-an-outside-source-and-keeps-the-last-word.md) — proposed; generalises the parallel source of [0034](0034-humus-policy-walk-llm.md) to items that are not blobs; learning follows the properties of [0037](0037-online-learning-loop.md); written references of [0073](0073-what-a-person-writes-is-a-link.md) stay local
- [0075 A proposal is read before it is decided, and a decision never loses anything](0075-a-proposal-is-read-before-it-is-decided.md) — accepted, phases 1 to 3 implemented (pruning and directing a link on the web open); one proposal contract over the mechanisms of [0032](0032-garden-classify-api.md), [0043](0043-human-gated-review-state-for-ai-generated-nodes.md), [0071](0071-the-graph-reads-the-notes-and-a-person-decides.md) and [0073](0073-what-a-person-writes-is-a-link.md); supersedes the "no `rejected` is stored" sentence of [0043](0043-human-gated-review-state-for-ai-generated-nodes.md)
- [0076 An agent checks and revises what a model proposed, and a person keeps the last word unless they hand it over](0076-an-agent-checks-and-revises-a-person-keeps-the-last-word.md) — accepted, phase A implemented; opens the reading of [0075](0075-a-proposal-is-read-before-it-is-decided.md) to assistants; carries out the delegated verification foreseen by [0043](0043-human-gated-review-state-for-ai-generated-nodes.md); phase B implemented (the last word handed over per kind of decision, migration 0037)
- [0077 One way to connect two things, notes or tasks, and the graph follows the work](0077-one-way-to-connect-two-things.md) — accepted, phases A and B implemented; one connection panel and vocabulary over the links of [0029](0029-note-garden-ecosystem.md), [0040](0040-mycelial-link-model.md), [0042](0042-tasks-as-graph-nodes-and-complete-autoclassify.md) and the written references of [0073](0073-what-a-person-writes-is-a-link.md); subtasks and dependencies in the unified weave; whether tasks enter the retrieval walk is open
- [0078 A session is what a person steers, and it says when it needs them](0078-a-session-is-what-a-person-steers.md) — accepted; a session entity above the worker of [0063](0063-possession-is-not-state.md), joined by a server-minted key so the credential predicate of [0072](0072-a-model-declares-work-done-only-as-the-checker.md) is unchanged; its operations on the registry of [0068](0068-one-operation-registry-for-rest-and-mcp.md); steering from the web, over the instructions of [0067](0067-an-instruction-is-a-comment-assigned-to-an-agent.md), left to the next record
