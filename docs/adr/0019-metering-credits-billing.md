# ADR-0019 Metering, credits wallet, rate cards, enforcement

Status: accepted. Reuses the bitvision_phoenix billing pattern
(see [references.md](../references.md)). Ties to ADR-0012 (LLM/Embedder
abstraction), ADR-0006 (secret envelope), ADR-0018 (S3 storage).

## Context

Users pay for what they consume. Billing is **credit-based**:
different models cost different credits; we convert tokens to credits
and decrement a balance. Cost basis differs by source. Storage is also
billed, with a different rate for DB vs S3. An admin tops up credits;
with no credits the system must stop metered work.

## Decision

- **Credit** is the internal billing unit. Per-org **wallet**
  (balance) plus an **append-only, idempotent `credit_ledger`** (grants
  and debits). Append-only enforced at DB level (same trigger pattern
  as `activity_log`, docs/adr/0002). Debits are idempotent by an
  operation id; balance changes use an **atomic check-and-debit**
  (`SELECT ... FOR UPDATE`) to prevent the overdraft race under
  concurrency.
- **Rate cards**, DB-driven registry (reuse bitvision `LLMRateCard`):
  per `model_id` -> provider, credits per input/output token, optional
  provider-cost basis + markup, `is_active`, tier. Separate **storage
  rates**: distinct configurable credits per GB-month for DB and for
  S3.
- **Cost basis**:
  - Local model: credits straight from the rate card (our price; no
    provider cost).
  - Our API key: provider cost (when returned) x markup -> credits.
  - **BYOK** (user's own OpenAI/other key): we do NOT bill provider
    cost; we bill a configurable **platform fee** = factor x metered
    units (e.g. 0.0001 x tokens). The user key is stored encrypted
    (ADR-0006 envelope).
- **Metering**: every metered operation writes a `usage_record`
  (model/op, tokens in/out or bytes-time, computed credits) and posts
  a ledger debit. Token counts come from the provider response or a
  local tokenizer.
- **Enforcement** in the core service layer (single choke point, like
  RBAC): before a metered operation, check sufficient balance; if not,
  reject with an i18n error code (ADR-0017). Gate **only
  cost-incurring** operations (LLM, embeddings, advisory using an LLM,
  new heavy storage writes). Read access, data/GDPR export, and
  retrieval of legal/compliance artifacts (invoices, conserved
  documents) remain available at zero credits (legal/ethical
  correctness; exact scope is a product decision).
- **Storage**: heavy attachments/documents go to **S3** (DB keeps
  metadata + small indexable text only). DB and S3 storage are metered
  separately (recurring GB-time sampled by the worker) at distinct
  configurable rates.
- **Admin**: admin-role API/UI to grant credits (creates a ledger
  credit entry), edit rate cards, activate models, set the BYOK fee
  factor and storage rates. Tenant-scoped under RLS; admin actions
  audited.
- A payment gateway (real money -> credits) is **out of v1**; v1 =
  manual admin credit grants.

## Consequences

- LLM/Embedder/advisory/memory and storage become metered and gated;
  rate cards key on `model_id` (ADR-0012); storage metering spans
  DB and S3 (ADR-0018).
- Ledger append-only + idempotent debits + atomic check-and-debit are
  correctness-critical and must be tested (concurrency, retries).
- Phasing: a dedicated **Billing & metering core** phase precedes the
  AI-metered phases (memory/advisory, F6); metering hooks land with
  each metered subsystem. F1 (tasks/taxonomy) has no metered ops and
  can proceed independently.

## Alternatives rejected

- Provider-cost passthrough only: ignores local models and BYOK.
- Hard-blocking all access at zero credits: blocks access to one's own
  legal/fiscal data and GDPR export; rejected.
- Non-idempotent debits / balance without atomic guard: double-charge
  on retries and overdraft race under concurrency.
