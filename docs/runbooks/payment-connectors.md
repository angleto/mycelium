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

1. Create the connector in Settings → issuer profile → Payment connectors. Pick
   the provider in the create form; the fields differ per provider, because for
   Stripe the signing secret is issued by Stripe and has to be pasted in, while
   on the native contract Mycelium can mint one.
2. Copy the `webhook_url` the connector shows and paste it into Stripe:
   Developers → Webhooks → Add endpoint.
3. Subscribe to **exactly the events the connector lists** under *How to
   configure stripe*, next to the URL.

   That list is generated from the connector's own settings by the same mapper
   that will receive the traffic — it is not transcribed here on purpose. A
   checklist in a runbook drifts from the code that reads the events, and both
   ways of being wrong are silent: too few events and documents never appear,
   the wrong refund event and one refund becomes two credit notes. The API
   serves it as `subscription` on the connector, so `GET
   /issuer-profiles/{id}/payment-connectors` answers the same question outside
   the SPA.
4. Stripe shows a signing secret (`whsec_…`). Paste it into the connector with
   *Rotate signing secret*. Until then the connector **cannot be enabled**
   (`payment_connector.signing_secret_missing`) and refuses every delivery: a
   connector with no secret is not one that is "not receiving", it is one that
   cannot verify anything.

   This order is forced by the provider and is why the secret is **not** asked
   for at creation: the URL contains the connector id, so the connector has to
   exist before the URL does, and Stripe issues the secret only once that URL is
   registered as an endpoint.

   Do **not** arm an *ingress API key* on a Stripe connector — the option is not
   offered, and the API refuses it (`payment_connector.ingress_key_unsupported`).
   A Stripe webhook endpoint sends what Stripe decides to send and has no field
   for a custom header, so the key would be configured, never presented, and
   every delivery refused. The second factor exists for senders that can carry
   it, which today means our own contract.
5. Nothing to set for the codice destinatario. `0000000` cannot be used to
   deliver, so there is deliberately no connector-wide default: a document is
   emittable when the customer supplied a real 7-character code or a PEC. A
   counterpart outside Italy is addressed by the standard's `XXXXXXX`, which
   the connector applies by rule.
6. Turn `enabled` on and send a test event from Stripe.

### The customer events are not optional

`customer.created` and `customer.updated` are on the generated list for every
Stripe configuration, and they are the entry an operator is most likely to skip
because nothing about them mentions invoices.

A Stripe webhook payload **cannot be expanded**. An `invoice.paid` names its
customer by bare id, so the VAT number, codice fiscale, codice destinatario and
PEC that live on the Stripe customer never travel with the money. These two
events are the only channel that carries them. Omit them and the connector
verifies, ingests and queues normally, then parks nearly everything as
`no_billing_data` — healthy-looking, and emitting almost nothing.

This bites hardest on the codice destinatario, because Stripe has no field for
one: it has to go in the customer's **metadata**, which is exactly the part that
does not travel with an invoice event. So "the customer plainly has it and
Mycelium says it is missing" is the expected symptom of the missing
subscription, not a bug in the reading.

Only the METADATA is read for it. In particular `address.line2` is **not**:
customers do type a codice destinatario there, because it is the only free
field the checkout offers them, but a second address line is an address line.
Reading it would mean deciding that a 7-character string in a street field is a
fiscal routing code — a guess about somebody else's data, on the field that
decides where an invoice is delivered. The cost is that a legitimate `line2`
("Interno 3", "Scala B") is dropped rather than appended, which is the safer of
the two errors.

The key it is stored under is configurable per connector
(`metadata_sdi_keys`, default `codice_destinatario`, `sdi_code`, `sdi`) and
matching ignores case and separators, so `Codice Destinatario`,
`codiceDestinatario` and `CODICE_DESTINATARIO` all resolve. The same holds for
the VAT number, the codice fiscale and the PEC.

### The causale comes from the connector, never from the provider

`default_purpose` is the `Causale` of every document this connector composes,
and it is the only source. The provider's own free text is not read for it.

That is a correction, not a limitation (ADR-0054). Stripe's invoice
`description` is the field the dashboard labels **Memo**: text the merchant
writes *to the customer*, rendered on the hosted invoice and on the PDF. On a
live account it held onboarding instructions, and those were filed verbatim as
the fiscal causale of a document kept in ten-year conservazione. `Causale` is
read by SdI, by the customer's commercialista and by an auditor years later.

The provider's text is not discarded: it still describes the supply on the line
`<Descrizione>`, which is where a description belongs.

Leaving `default_purpose` empty is valid — `Causale` is `<0.N>` in the
tracciato, and a document without one is perfectly conformant. On a forfettario
issuer the statutory L.190/2014 dicitura is emitted regardless, as an additional
`Causale`, and can no longer be displaced by anything anyone types.

### Retry, Recompose, Rebuild XML: three verbs, and only one of them rebuilds

They are not variations on each other and the difference is fiscal, so the
buttons are only offered where the server will honour them.

**Retry** re-arms a parked or dead event. If the event never composed anything
it runs from scratch through today's mapper, which is what you want. If it
ALREADY composed a document, retry does not rebuild it: the object claim
short-circuits into settle, which on a `transmit` connector files the existing
draft as it stands. The button therefore reads **Transmit draft** on those rows
and says so before doing it.

**Recompose** (owner) deletes the composed document, drops its claim and re-runs
the frozen payload through the mapper. Use it after a fix that changed how the
document is BUILT from the event. It refuses anything but an untouched draft:
a number, a file name or a frozen XML means a send was attempted, and deleting
that draft would burn a fiscal number and destroy the NomeFile dedupe that makes
a resend safe. It also refuses when a second event points at the same document,
because deleting it would detach that one silently.

**Rebuild XML** (owner) re-shoots the frozen *shadow* document. It exists
because `dry_run_xml` is the only XML the subsystem stores.

The case that needs none of them: a fix to the SERIALIZER. A draft holds no
XML at all -- `invoices.xml` is NULL until transmit freezes it, and the preview
is rebuilt from the current rows on every read -- so opening a pre-fix draft
already shows post-fix bytes. What a serializer fix does NOT repair is
persisted column data: a causale or a payment method written onto the row when
it was composed. Those are edited on the draft itself.

### A connector states a payment method only if you set one

Leave `default_payment_method_code` empty and the composed document carries no
`DatiPagamento` block at all. That is legal: block 2.4 is `<0.N>` in the AdE
tracciato and no SdI control reads it.

Set it (`MP08`, carta di pagamento, for a card processor) and the block is
emitted complete, with the conditions code alongside it — `TP02` unless you set
another. The conditions code cannot be set on its own: the configuration is
refused, because it used to open the block while leaving the method to resolve
to the system default `MP05` (bonifico), stating on a fiscal document that a
card charge was a bank transfer.

For the same reason a connector-composed draft does **not** inherit the issuer
profile's `default_iban`. If your issuer profile is the one you also use for
hand-written bonifico invoices, that IBAN stays on those and off these.

Subscribing the two events afterwards is enough: parked payments re-arm
themselves the moment a `customer.*` event arrives carrying what they were
waiting for. Stripe does not replay old customer events, so touch the customer
(any edit fires `customer.updated`) or wait for the next change.

### One emission trigger, one refund announcement

Stripe announces the same money more than once, in both directions, and the
connector deliberately acts on ONE announcement of each.

**Emission.** An `invoice.paid` is also a `charge.succeeded` and a
`payment_intent.succeeded`. `emission_event` names the trigger and the others
are demoted to payment reconciliation, so subscribing several is safe. Changing
`emission_event` while events for the same payment are still in the provider's
retry queue can still produce an invoice from the old trigger and a second claim
attempt from the new one — the object links stop the second document, but the
event lands in the ledger as a duplicate. Change the trigger during a quiet
window.

**Reversal.** `refund.created` and `charge.refunded` describe the same refund,
and unlike the emission family they do **not** always deduplicate: they agree
only while the charge payload carries expanded `refunds.data`. Recent API
versions do not expand it, and `charge.refunded` cannot then know the refund id,
so it claims a key derived from the charge, which collides with nothing. Acting
on both would file two TD04 for one refund.

So `refund_event` selects one, and the connector ignores the other with
`refund_event_not_selected` recorded on the event. `refund.created` is the
default and the right choice; pick `charge.refunded` only for an endpoint that
predates the newer event. If refunds are not reversing, check this field before
anything else: the ignored events are listed under *Ignored* in the connector's
event tabs, with the reason on each row.

**Fixing it later does not replay them.** `ignored` is deliberately not a
retryable state, and a Stripe "Resend" does not help either — the redelivery
carries the same event id, which the ingress dedups. So refunds that arrived
while the wrong announcement was selected have to be credited by hand
(Invoices → the parent document → credit note).

That is the conservative answer on purpose. Re-arming them automatically is
only safe when nothing reversed those refunds already, and the system cannot
tell: the two announcements claim DIFFERENT keys for one refund — which is the
whole reason this setting exists — so the emission-idempotency ledger would not
catch a second TD04 for a refund the other announcement had already reversed.
Getting the field right before traffic starts is much cheaper than either
outcome; the connector's setup guide lists the announcement it expects.

## Amounts and VAT

`vat_pricing` decides how a provider figure is read. It is **not** a yes/no,
because the payload usually answers the question itself:

- **`auto`** (default). When the provider states a tax behaviour — Stripe does
  whenever it computed tax, as `tax_behavior` (2026-07-29 API) or `inclusive`
  (before it) — that statement wins. It describes money that actually moved.
  When the payload states **nothing**, the amount is read as VAT-**inclusive**:
  it is money already collected, so it is the document total, and reading it as
  net would add VAT on top and invoice more than the customer paid.
- **`gross` / `net`**: force, ignoring the payload. Only for a feed whose own
  tax flags are known to be wrong; wrong by construction otherwise.

A `charge` or `payment_intent` amount is gross under every setting: the card was
debited exactly that, which is arithmetic and not a convention.

On the native contract a sender states it per line with `price_includes_vat`,
and is believed under `auto`.

**If your Stripe account is on the 2026-07-29 API or later**, note that the line
tax moved from `tax_amounts`/`inclusive` to `taxes`/`tax_behavior`, with the
rate identified only by id. Both shapes are read, and the aliquota is recovered
from the reported tax and its taxable base by finding the statutory rate that
reproduces it (2049 x 22% = 450.78 -> 451). That is a verification, not a guess:
the raw quotient would give 22.0107, which is arithmetically defensible and
fiscally wrong.

## Where to read what a connector did

Configuration lives in Settings → issuer profile → Payment connectors: create,
credentials, modes, defaults, and the generated event list.

Everything the connector PRODUCED is read from **Invoices → Automated
collections**: events waiting for billing data, events needing a decision,
ignored events with their reason, and the inbound delivery ledger. That is daily
work on fiscal documents, so it sits with the documents rather than in a
settings page you visit once.

## What an unauthenticated caller can cost you

The ingress is public by construction: a provider posts to it with no bearer and
the authority is the HMAC over the raw body. Nobody without the signing secret
can get a single event ingested, and that has always been true.

What is bounded now is the WRITE. Every refused delivery appended a row to the
delivery ledger, so anyone who learned a connector URL could grow that table
without limit (the URL is a v4 UUID — unguessable, but pasted into dashboards
and read off screens, so not a secret). A fixed-window counter caps how many
refusals per connector get recorded
(`MYCELIUM_PAYMENT_CONNECTOR_REFUSAL_BUDGET`, default 120 per
`..._REFUSAL_WINDOW_SECONDS`, default 60). Past the budget the caller still gets
the same 401 — it learns nothing about a limit existing — and only the ledger
append stops.

It counts refusals, never requests, so a legitimate burst is never affected: to
be accepted you must already hold the signing secret, and whoever holds it is
the provider.

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
- **Nothing can send them, not even by mistake.** The three places a connector
  can transmit are each guarded by its mode, and a test drives the whole shadow
  surface (emission, redelivery, payment reconciliation, refund) through an SdI
  channel that fails the test if it is called at all. On top of that, the single
  function that files refuses any document still marked `dry_run`
  (`invoice.dry_run_not_sendable`), so the SPA's transmit button and the public
  issuer-key API cannot file one either. Promotion is the only way out.
- **Each one says why it was not sent.** The document carries `dry_run`, shown
  in the invoice list as *not sent: dry run*. A draft is unsent for many
  reasons -- incomplete, waiting for review, rejected by SdI and being redone --
  and during a parallel run the only question that matters is which ones were
  held back because you are shadowing. That is a property of the document, not
  something to reconstruct from the ingress ledger.
- **Switching to `transmit` later emits fresh documents.** Shadow claims are
  separated by a column on `payment_object_links`, so a live lookup does not see
  them and does not resume a shadow draft. The shadow drafts survive as
  evidence.

### The two exits

When the comparison is done, every shadow document leaves shadow state one of
two ways. Nothing expires on its own.

**Discard** -- *Discard shadow run* (`POST .../discard-dry-run`), per connector.
The right answer for everything the incumbent provider already invoiced: it
throws away the claims and the drafts together. Not a per-invoice delete: the
shadow claims hold a RESTRICT foreign key on their drafts, so deleting a draft
directly fails. The operation drops the claims first, then the drafts, never
touches a live claim, and leaves the generated XMLs on their events.

**Promote** -- *Make sendable* (`POST .../events/{id}/promote`), per document.
The right answer for a payment Mycelium has to invoice after all: the incumbent
missed it, or you are cutting over without waiting for the next event. The
document leaves the archive, loses the `dry_run` marker and returns to the
sendable drafts, where you transmit it with the ordinary action.

Promotion moves the claim rows with the document, out of the shadow universe
and into the live one, in the same transaction. That part is not cosmetic: a
promoted document whose claim stayed in the shadow universe would be invisible
to a live lookup, so the next redelivery of that payment would compose a
**second** invoice for money already invoiced. For the same reason, promotion is
refused (`payment_connector.already_emitted`) when a live document already
covers that payment -- switch the connector to `transmit` first and you may find
the leftover shadow is exactly that case.

The fiscal number is **not** allocated at promotion. It is allocated when the
document is really transmitted, so a promoted document that you decide against
after all has still spent nothing.

Two things shadow mode does not cover:

- **credit notes.** A TD04 corrects an emitted document and in shadow mode
  nothing is emitted, so refunds park as `dry_run_credit_note_unsupported`.
  During a parallel run the incumbent is still issuing them. To keep the
  parked events out of your triage list, set `credit_note_mode = 'off'` for
  the duration of the trial.
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
side. Submitting the rotation WITHOUT a value is refused for a vendor provider
(`payment_connector.signing_secret_required`): minting there would install a
secret the provider never issued, leaving a connector that looks healthy while
refusing every delivery, with the working secret already demoted to the grace
copy. On the native contract the same empty submission is legitimate — Mycelium
is the authority — and mints one; a key agreed with the sender can be pasted
instead.

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
