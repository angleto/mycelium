# ADR-0047 Signed outbound webhooks on invoice state changes

Status: accepted (task 2c23e955; builds on ADR-0045 issuer-scoped keys and
ADR-0046 two-phase durable transmit).

## Context

Integrators polling `GET /api/v1/invoices` to learn when an invoice was
delivered / accepted / rejected is wasteful and laggy. They want a push: a
signed HTTP callback when an invoice changes fiscal state. The hard constraint
is that this is bolted onto a **live fiscal system** where real invoices are
already flowing (ADR-0046): the notification path must be provably unable to
affect the transmit / ingest write it observes.

## Decision

A per-issuer-profile **webhook endpoint** (owner-gated CRUD, mirrors the
issuer-key surface) with a **transactional outbox** delivered by a decoupled
worker.

1. **Emit is an outbox INSERT inside the fiscal transaction.** The six fire
   sites (`transmit` success, the `ingest_active_notification` transitions for
   RC/MC/AT/NS/NE/DT, `mark_paid`) call `enqueue_invoice_event`, which INSERTs
   `webhook_deliveries` rows with a FROZEN payload snapshot. So a delivery is
   durable iff the state change committed, and no network I/O touches the
   request path.

2. **Fiscal safety: the emit runs in a SAVEPOINT and swallows every error.**
   A bare INSERT that errors (a bad constraint, a serialization failure, a
   coding bug) would otherwise poison the whole Postgres transaction and roll
   back the fiscal write. `enqueue_invoice_event` wraps the fan-out in
   `session.begin_nested()` and catches all exceptions, returning 0. A webhook
   fault can never abort a transmit or a receipt ingest. This is the
   load-bearing invariant and it has a dedicated test.

3. **At-least-once, idempotent.** `UNIQUE(endpoint_id, dedupe_key)` +
   `INSERT ... ON CONFLICT DO NOTHING`. The dedupe key ties the event to its
   logical occurrence (`transmitted:{id}`, `{event}:{id}:{message_id}` for SdI
   notifications, `payment_recorded:{id}`), so the double-fire paths (SdI
   redelivery, lost-ACK reconcile) enqueue once. The receiver replay-guards on
   the stable `X-Webhook-Id` (the delivery row id). Fire only on an ACTUAL
   state transition (`inv.state != prev_state`) so a second RC does not re-fire
   `delivered`.

4. **Signed.** Each POST carries `X-Webhook-Signature: v1=<hex>` =
   HMAC-SHA256(secret, `{timestamp}.{body}`) plus `X-Webhook-Timestamp`, so a
   captured body cannot be replayed under a fresh signature and the receiver
   rejects a skewed timestamp. The signing secret is a **reversible Fernet
   envelope** (`crypto.encrypt_secret`, keyed by `MYCELIUM_SECRET_KEY`) — unlike
   an inbound bearer credential it must be recoverable to recompute the MAC.
   Rotation keeps the previous secret verifying for a grace window.

5. **SSRF-guarded, at BOTH create and send.** HTTPS only; the host is resolved
   and every answer must be public unicast (a single private answer rejects the
   name, defeating DNS rebinding). Re-resolving at send time closes the
   create-time-to-send-time rebinding window.

6. **Decoupled delivery worker** with an at-least-once lease: claims due rows
   `FOR UPDATE SKIP LOCKED`, marks `delivering` + `last_attempt_at`, POSTs,
   records the outcome with exponential backoff to `webhook_max_attempts`, then
   `dead`. A crashed in-flight lease older than `webhook_delivery_lease_seconds`
   is reclaimed. Registered only when `webhooks_enabled` (fail-closed: an
   unconfigured deploy never runs the loop, so the fiscal path is untouched).

## Payload & privacy

The snapshot is whitelisted and PII-lean: invoice id, number, series, year,
document type, state, sdi_status, IdentificativoSdI, buyer verdict, payment
status, total, `client_tag_id`, `issuer_profile_id`, event, occurred_at. No raw
XML, no cessionario address/PEC — the receiver correlates on `client_tag_id` and
pulls detail from the REST API under its own key. Because an invoice is a fiscal
record and is never hard-deleted (`taxonomy.purge_client` refuses a client with
invoices), the snapshot's retention equals the invoice's own; a TTL sweep
(`webhook_delivery_retention_days`) caps the delivery-log growth, and the
`invoice_id` FK is `ON DELETE SET NULL` as a theoretical safety net.

## Consequences

- Enabling requires a worker restart and setting `webhooks_enabled`; events
  during a disabled window are dropped, not back-filled (no historical replay in
  v1).
- Holding one tenant transaction open across a bounded batch of sequential POSTs
  is accepted for v1 low volume (batch × per-POST timeout bounds it); a future
  claim-then-send split would remove even that.
- Config knobs: `webhook_delivery_timeout_seconds`, `webhook_delivery_lease_seconds`,
  `webhook_max_attempts`, `webhook_backoff_base_seconds` / `_cap_seconds`,
  `webhook_poll_interval_seconds`, `webhook_delivery_retention_days`.
