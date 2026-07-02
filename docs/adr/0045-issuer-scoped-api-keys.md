# ADR-0045 Issuer-scoped API keys, public Invoice API, MCP invoice tools

Status: accepted. Task 19b7e874, phases 1-5.

## Context

A workspace invoices under one or more issuer profiles (cedente / VAT
subjects, ADR-0011) and may serve thousands of clients. Invoice compose /
send / track was reachable only through the human SPA session (JWT +
`X-Workspace-Id`) and org-wide MCP agent tokens. Two needs:

1. A machine-to-machine REST API for external integrators, authenticated by a
   credential scoped to a single issuer profile, with a full key lifecycle.
2. Agent access over MCP ("controlla l'ultima fattura del cliente XY").

The invoicing core (FatturaPA build, XSD, per-issuer numbering, SdICoop channel,
`SdiMandate`, immutability, notification ingest) is mature; this is an access
layer, not new fiscal logic. It carries live fiscal traffic, so the guiding
constraint is: add capability without changing the existing transmit semantics.

## Decision

**A dedicated `issuer_api_keys` table, not an extension of `agent_tokens`.**
`agent_tokens` mandates a user FK (cascade on user delete), carries one coarse
scope, has no rotation, and is the internal MCP-transport identity. An external
fiscal credential owned by an issuer profile is a different lifecycle and
audience, so it gets its own table keyed on `issuer_profile_id` (cascade),
`created_by` set-null (audit only, not ownership).

**Credential hardening** (verified by an adversarial review):
- system-generated 256-bit secret, shown once; stored ONLY as
  `HMAC-SHA256(ISSUER_KEY_PEPPER, raw)` under a dedicated, key-separated,
  fail-closed pepper, so a database-only dump cannot verify a candidate secret;
- a non-secret `key_public_id` from an independent draw is the display handle
  (never a slice of the raw);
- a SECURITY DEFINER `authenticate_issuer_api_key` verifies in-DB: two-probe,
  current-secret-wins, the rotation grace scoped to the previous-hash branch
  (never the shared gate, which would self-DoS the current secret), a single
  `revoked_at` killing both secrets, and a throttled last-used bump;
- rotation-with-grace (default 0 = hard), and a mandatory expiry capped at 365
  days (no never-expiring key), with 30/7-day warnings.

**Authorization** is a pure function of the key's permissions plus a pinned
`member` role: `issuer_key_ctx` clones the capability-token dependency (not
`quick_create`/`admin_session`) and pins `app.current_role=member`, so the grant
is independent of the minting user's live role or deletion (H1). Issuer isolation
is enforced by one centralized guarded loader that returns 404 (never 403) on a
cross-issuer id (H2) -- row-level security gives org isolation, not issuer
isolation.

**The issuer key authenticates the public REST surface (`/api/v1`) only.** MCP
stays on the org-scoped agent-token principal (co-equal REST/MCP, ADR-0001) and
gains read tools plus the now-wired per-tool scope gate (`invoices:read` /
`invoices:write`). Key minting is REST + GUI only (the same
credential-configures-the-transport carve-out as agent tokens).

**Recipients** accept both a stored `client_tag_id` and inline cessionario data
resolved-or-created idempotently, but inline creation needs a distinct
`invoice:client_write` permission on a member-authorized path -- not the
admin-gated client CRUD (confused-deputy fix).

**Fiscal safety**: a mandatory client `Idempotency-Key`, claimed atomically in
the mutation's transaction, prevents double-filing under retry; a per-key
shared-store (Postgres) rate limit bounds cost on the expensive verbs; the
emitted `ProgressivoInvio` / `NomeFile` are recorded on the invoice.

## Consequences

- The pepper cannot be rotated without re-minting every key (no raw is stored):
  treat it as long-lived secret-manager material, held out of the DB/backups.
- Full lost-ACK durability shipped as ADR-0046 (two-phase transmit: the fiscal
  identity — numero, `ProgressivoInvio`/`NomeFile`, frozen XML — is committed
  BEFORE dispatch, a retry re-sends the same bytes under the same name, and
  the inbound reconcile correlates by `NomeFile`). The blockers noted at
  deferral time (the request-long row lock and the transaction-local RLS
  GUCs) are resolved by `tenant_checkpoint` on a checkpoint-friendly
  `tenant_session`, not by a nested session.
- Deferred follow-ons: signed outbound webhooks, optional per-key IP allowlist,
  full security alerting, a binary PDF download over MCP (the XML is text and
  ships inline; the PDF is on `/api/v1`), a per-issuer least-privilege allowlist
  over MCP.

## Alternatives rejected

- Overloading `agent_tokens` with a nullable `issuer_profile_id`: mixes an
  external fiscal credential with the internal MCP transport, inherits the user
  cascade and the single coarse scope, and has no rotation.
- Bare `sha256(raw)` at rest (as the inherited agent-token hash does): a DB dump
  plus a candidate list can verify keys offline; the pepper closes that.
- Enforcing the MCP per-tool scope gate globally in one step: too broad a blast
  radius; wired for the invoice tools first, legacy bare tokens kept at full
  access.
