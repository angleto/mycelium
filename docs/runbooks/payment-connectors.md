# Runbook: inbound payment connectors (ADR-0051)

Operating a connector that turns a payment provider's webhooks into electronic
invoices. For the decision and its rationale see `docs/adr/0051-inbound-payment-connectors.md`;
for the published event format see `docs/payment-connector-contract.md`.

## Enabling the subsystem

Nothing is fleet-wide. What a connector may do is decided **per issuer
profile**, on the connector row itself:

| Setting | Scope | Effect |
| --- | --- | --- |
| the connector's `enabled` flag | one connector | off = the ingress answers 403 after verifying the signature |
| `invoice_mode` | one connector | `transmit` files it, `dry_run` composes AND builds the XML but never sends, `draft` composes and stops, `off` parks it |
| `credit_note_mode` | one connector | the same four, independently: automating invoices while keeping storni manual is a normal posture |
| `MYCELIUM_PAYMENT_CONNECTORS_ENABLED` | the whole deployment | **on by default**; the kill switch, see below |

A connector is created **disabled**, and it cannot exist without the provider's
own signing secret, so nothing can be emitted by a connector nobody
deliberately created and armed. That is where the fail-closed guarantee lives —
not in a fleet-wide flag that costs a worker restart to flip and can leave a
correctly configured connector silently inert, answering 404 as if it had never
been created.

`MYCELIUM_PAYMENT_CONNECTORS_ENABLED=false` remains the emergency lever: the
ingress 404s for every connector and the worker loop is not registered.
Flipping it either way requires a **worker restart** (the loop is registered at
startup). Events that arrive while the loop is down are not lost — they queue
in `payment_connector_events` and drain when it comes back.

`MYCELIUM_PAYMENT_CONNECTOR_BASE_URL` is the origin the connector's webhook URL
is advertised under — the address an operator pastes into the provider's
dashboard, so it must be the API origin. It falls back to `frontend_base_url`,
which is correct only where the SPA and the API share a host; set it
explicitly rather than relying on that coincidence.

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

## Running in parallel with an incumbent provider (`dry_run`)

Before cutting over, put the connector in `invoice_mode = 'dry_run'` and leave
it there for a full billing cycle. It does everything a real emission does --
resolves the counterpart, composes the lines, computes the totals, builds the
FatturaPA and validates it against the official XSD -- and stops before SdI.

What you get per payment: a **frozen XML** on the event, downloadable from the
connector's event list (the `XML` button on the `Processed` tab). Diff it
against what your current provider filed for the same payment. That is the
whole exercise.

What it costs you: nothing that cannot be undone.

- **No fiscal number is spent.** The XML carries a would-be number and the
  `ANTEPRIMA` progressivo, so the counter is untouched.
- **Nothing is sent**, and nothing is marked paid.
- **The shadow documents are archived**, out of the active invoice list, so
  they cannot be transmitted by accident. They stay inspectable (XML, PDF,
  totals) in the archived view.
- **Switching to `transmit` later emits fresh documents.** Shadow object
  claims live in their own `dryrun:` namespace, so a live run does not find
  them and does not resume a shadow draft. The shadow drafts survive as
  evidence. To clear them use **Discard shadow run**
  (`POST .../discard-dry-run`), not a per-invoice delete: the shadow claims
  hold a RESTRICT foreign key on their drafts, so deleting a draft directly
  fails. The operation drops the claims first, then the drafts, and never
  touches a live claim. The generated XMLs stay on their events.

Two things it does not cover:

- **credit notes.** A TD04 corrects an emitted document and in shadow mode
  nothing is emitted, so refunds park as `dry_run_credit_note_unsupported`.
  During a parallel run the incumbent is still issuing them.
- a document that fails validation parks as `dry_run_invalid_document` with
  the validator's message. That is the finding, not a fault: fix the data and
  retry.

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

**When the data arrives outside the provider.** The self-service path only
fires when the provider tells us: a `customer.created` / `customer.updated`
event carrying the fiscal identity fills the client record and wakes the
payments waiting on it. If the customer emails you their VAT number instead,
fixing the anagrafica is *not* enough — nothing ties that mycelium client to
the provider's customer id, so **Retry alone will park the payment again**, for
the same reason as the first time: it re-derives the counterpart from the
frozen event payload.

Use **Assign client** on the parked row instead:

1. make sure the client exists in Clients and is complete (VAT number or
   codice fiscale, full address, and a codice destinatario or PEC);
2. press *Assign client* on the parked payment and give it the client's tag id.

The association is refused if the client is still incomplete, and it names what
is missing — so the failure lands on the record you are looking at rather than
on a retry later. On success every payment waiting on that customer is queued
again with a fresh attempt budget, and future payments from the same customer
resolve straight through.

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
