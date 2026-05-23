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
- [0008 No-ubiquity: events entity](0008-no-ubiquity-events.md)
- [0009 Invoice immutability, soft-delete carve-out](0009-invoice-immutability.md)
- [0010 Conservation: free AdE service](0010-conservation-ade-free-service.md)
- [0011 SDI: intermediary/mandate model, v1 B2B/B2C](0011-sdi-intermediary-mandate-v1-b2b.md)
- [0012 LLM/Embedder abstraction, reuse the bitvision pattern](0012-llm-embedder-abstraction.md)
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
