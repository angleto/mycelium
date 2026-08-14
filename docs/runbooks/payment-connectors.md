# Runbook: inbound payment connectors (ADR-0051)

Operating a connector that turns a payment provider's webhooks into electronic
invoices. For the decision and its rationale see `docs/adr/0051-inbound-payment-connectors.md`;
for the published event format see `docs/payment-connector-contract.md`.

## Enabling the subsystem

Two switches, both fail-closed, and both must be on:

| Setting | Effect when off |
| --- | --- |
| `MYCELIUM_PAYMENT_CONNECTORS_ENABLED` | the public ingress answers 404 for every connector |
| the connector's own `enabled` flag | the ingress answers 403 after verifying the signature |

Enabling the fleet switch requires a **worker restart**: the processing loop is
registered at startup. Events that arrive while the loop is down are not lost —
they queue in `payment_connector_events` and drain when it comes back.

`MYCELIUM_PAYMENT_CONNECTOR_BASE_URL` is the origin the connector's webhook URL
is advertised under. Set it to the API origin in production; it falls back to
`frontend_base_url`, which is only correct where the SPA and the API share a host.

## Wiring a Stripe account

1. Create the connector in Settings → issuer profile → Payment connectors.
   Copy the `webhook_url` it shows.
2. In Stripe: Developers → Webhooks → Add endpoint, paste the URL, and subscribe
   to the events you actually need:
   - the emission trigger (`invoice.paid` by default — **exactly one**, see below),
   - `credit_note.created`, `charge.refunded`, `refund.created` for reversals,
   - `charge.succeeded` if you want payment reconciliation on documents the
     connector composed as drafts.
3. Stripe shows a signing secret (`whsec_…`). Paste it into the connector.
4. Nothing to set for the codice destinatario. `0000000` cannot be used to
   deliver, so there is deliberately no connector-wide default: a document is
   emittable when the customer supplied a real 7-character code or a PEC. A
   counterpart outside Italy is addressed by the standard's `XXXXXXX`, which
   the connector applies by rule.
5. Turn `enabled` on and send a test event from Stripe.

### Never subscribe to two emission triggers

Stripe fires several events for one payment: an `invoice.paid` is also a
`charge.succeeded` and a `payment_intent.succeeded`. The connector treats exactly
one type as the trigger (`emission_event`) and demotes the others to payment
reconciliation, so subscribing to all of them is safe. But changing
`emission_event` while events for the same payment are still in the provider's
retry queue can produce an invoice from the old trigger and a second claim
attempt from the new one — the object links stop the second document, but the
event lands in the ledger as a duplicate. Change the trigger during a quiet
window.

## The two ledgers

Both are per-connector and readable from the SPA.

- **Deliveries** (`payment_webhook_deliveries`) — one row per inbound HTTP
  request, accepted or refused, with the SHA-256 of the exact bytes received.
  This is what answers *"the provider says it delivered this and we have no
  invoice"*. Filter to refused-only for the security view.
- **Events** (`payment_connector_events`) — one row per event we agreed to act
  on, with the frozen payload and its processing outcome.

A request that names a connector id which does not resolve leaves **no** row in
either: there is no tenant to attribute it to. Those appear only as
`payment_connector.unresolved` in the security log.

## The waiting room vs. the queue

Two parked states, deliberately distinct, because they say opposite things about
whose move it is.

### `no_billing_data` — the customer's move

The counterpart never supplied a complete billing block. Nothing is broken and
there is nothing for you to decide. **These re-arm themselves**: the moment a
`customer.created` / `customer.updated` event arrives carrying the missing
fiscal identity, every payment waiting on that customer goes back to `pending`
with a fresh attempt budget, and the invoice is emitted without anyone pressing
anything. Subscribe to the customer events in the provider or this never fires.

`error_detail` names the missing fields:

| Detail | Meaning |
| --- | --- |
| `vat_number\|tax_code` | no P.IVA and no codice fiscale |
| `sdi_code\|pec` | no codice destinatario and no PEC. `0000000` is **not** a substitute: it cannot be used to deliver |
| `address` / `postal_code` / `city` | the postal address is incomplete |

A counterpart outside Italy never needs a codice destinatario: the connector
addresses it with the standard's `XXXXXXX` by rule.

### `needs_attention` — your move

The event is valid but a decision is pending. Deliberately terminal rather than
retried: the condition will not resolve on its own.

| `last_error` | What happened | Fix |
| --- | --- | --- |
| `client_rejected` | the fiscal identity was present but invalid (e.g. a malformed P.IVA) | correct it at the provider, then Retry |
| `payload_invalid` | the body is not a well-formed event for this provider | nothing to retry; the sender must resend correctly |
| `invoice_mode_manual` / `credit_note_manual` | automation is switched off for this document type | emit the document by hand from the Invoices page, or change the mode |
| `parent_not_transmitted` | a refund arrived for an invoice still in draft | transmit the parent, then Retry |
| `parent_rejected` | the parent was scartato by SdI | a scartato invoice is corrected by resend (`reopen_rejected`), not by a credit note — decide by hand |

`status = dead` means the attempt budget ran out on a condition that kept
deferring. Retry re-arms it with a fresh budget.

**Retry is safe.** Re-running an event that already produced a document resolves
to that document instead of emitting a second one: the object links are claimed
and committed before anything is filed.

## Rotating credentials

Both credentials rotate with a grace window
(`MYCELIUM_PAYMENT_CONNECTOR_SECRET_GRACE_HOURS`, 24h), so events already sitting
in the provider's retry queue keep verifying under the old secret.

**Signing secret.** Rotate at the provider first, then paste the new value into
the connector — in that order, or the window covers nothing. Stripe supports two
live signing secrets during a rollover, which is the same window from the other
side.

**Ingress API key.** Rotate in Mycelium, then update the sender within the grace
window. Clearing it removes the second factor entirely; the MAC remains mandatory.

The ingress key is stored as a one-way peppered hash under
`MYCELIUM_ISSUER_KEY_PEPPER` (shared with issuer keys, domain-separated in the
hashed message). Rotating that pepper therefore invalidates connector keys too —
see `docs/runbooks/issuer-key-pepper.md` and re-mint both families together.

## Retention

The worker sweeps terminal rows past
`MYCELIUM_PAYMENT_CONNECTOR_EVENT_RETENTION_DAYS` (730 by default).

- `no_billing_data`, `needs_attention` and `dead` events are **never** swept.
- `payment_object_links` is **never** swept. It is what stops a very old
  redelivery from emitting a second document, so it must outlive every event.
- The invoice itself is the durable fiscal record and is untouched by any sweep.

## Operational notes

- **Refusal flood.** Every refused delivery against a *resolved* connector writes
  a ledger row, so a caller who knows a connector id can grow that table without
  a valid signature. Bound it at the edge (nginx rate limiting on
  `/api/v1/connectors/`) rather than by dropping the audit trail; retention caps
  the long-run size.
- **The lease.** `MYCELIUM_PAYMENT_CONNECTOR_LEASE_SECONDS` (600) must stay above
  `sdi_dispatch_timeout_seconds` + `sdi_dispatch_lease_seconds` (120 + 300). An
  expired lease is treated as proof that nothing is still in flight; shrinking it
  below that sum breaks the proof and risks a double dispatch.
- **Manual export.** With no SdI channel configured the default is
  `ManualExportChannel`: a "transmitted" invoice gets a fiscal number but no
  `identificativo_sdi`, and no SdI notification will ever arrive to move it
  forward. That is expected in staging, not in production.
