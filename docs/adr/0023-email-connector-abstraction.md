# ADR-0023 Email connector abstraction and idempotent sync

Status: accepted. Follows the abstraction pattern of ADR-0012
(LLM/Embedder) and the security model of ADR-0006.

## Context

FR-7 requires Gmail (OAuth2 XOAUTH2), generic IMAP/SMTP, and later
Proton Bridge. The provider differs only at the network boundary
(authentication and transport); the domain logic (idempotent ingest,
threading, email-to-task, reply-in-thread) is provider-agnostic.
Credentials (OAuth refresh tokens, IMAP passwords) are opaque secrets:
ADR-0006 mandates an app-level envelope for non-indexed secrets, not
volume encryption alone.

## Decision

- A single `EmailConnector` Protocol with neutral DTOs
  (`FetchedMessage`, `OutgoingMessage`); a DB-driven factory selects
  the concrete connector by `email_accounts.provider`. Same shape as
  ADR-0012: Protocol + factory + neutral DTOs.
- The concrete `ImapSmtpConnector` covers generic IMAP/SMTP and Gmail
  (XOAUTH2 differs only in the SASL string). Proton Bridge is the same
  connector pointed at the local Bridge endpoint (post-Gmail, opex
  documented).
- The connector is injected into the sync/send service, so the
  external IMAP/SMTP boundary is a seam: tests use an in-memory
  connector implementing the Protocol. This is the same legitimate
  seam as the LLM provider, not mocking domain logic.
- Sync is idempotent: `email_messages` carries
  `(account_id, provider_message_id)` UNIQUE; re-ingesting a message
  is a no-op. Per-account fault isolation: one account's failure sets
  its status/last_error and never aborts the others.
- Opaque secrets are stored through a Fernet envelope
  (`flow_core.crypto`, key from `FLOW_SECRET_KEY`, fail-closed). The
  DB column holds ciphertext; the API never echoes the secret.
- Reply stays in-thread via `In-Reply-To`/`References`. No
  server-side folder/label management in v1 (FR-7).

## Consequences

- Adding a provider = a new connector class + a factory branch; the
  domain, RLS, and tests are untouched.
- Secrets survive a stolen volume/snapshot and are not readable from a
  live DB connection without the app key (ADR-0006 threat model).
- Network connectors are not exercised in CI; the deterministic core
  (idempotency, threading, email-to-task, isolation) is.

## Alternatives rejected

- Provider-specific code paths in the service layer: duplicates domain
  logic and breaks the co-equal REST/MCP adapters (ADR-0001).
- Storing credentials under volume encryption only: readable from a
  live connection; violates ADR-0006 for opaque secrets.
- Mocking the service in tests instead of injecting a connector:
  would stop testing the real ingest/threading logic.
