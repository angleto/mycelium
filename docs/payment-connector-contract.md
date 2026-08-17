# Mycelium payment connector contract (provider `mycelium`)

This is the **published event contract** of a Mycelium inbound payment
connector. A system that implements it can turn its own money events into
Italian electronic invoices (FatturaPA, filed with SdI) without an adapter
being written for it inside Mycelium, and without a third party seeing the
invoices.

It is one of two providers a connector can be configured with. `stripe`
reads Stripe's event shape; `mycelium` -- this document -- is our own format,
a thin JSON projection of the neutral DTOs the engine actually consumes.
Everything below is derived from
`core/src/mycelium_core/services/payment_native.py` and the ingress in
`api/src/mycelium_api/routers/connector_webhooks.py`.

Design rationale (why the ingress is asynchronous, why a wrong document is
worse than a parked event, why the ledger keeps a digest) is in
[ADR-0051](adr/0051-inbound-payment-connectors.md). This document is the
reference: what to send, and what happens to it.

---

## 1. The endpoint

```
POST {base}/api/v1/connectors/mycelium/{connector_id}
Content-Type: application/json
```

- `{connector_id}` is the connector's UUID. It is a **routing selector, not
  a credential**: authority comes from the signature. The full URL is shown
  in the workspace UI when the connector is created (`webhook_url`), ready
  to paste into your sender's configuration.
- The path segment `mycelium` must match the connector's `provider`. A
  connector created for `stripe` addressed under `/mycelium/` answers 404,
  and vice versa.
- The body must be a JSON **object**. An array, a bare string or malformed
  JSON is refused.
- Maximum body size: **1 MiB** by default
  (`payment_connector_max_body_bytes`; the limit is checked on the real
  byte length, not on `Content-Length`).

A successful call answers `200` with:

```json
{"received": true, "duplicate": false}
```

`duplicate: true` means this event id had already been accepted. It is a
success, not an error -- see [§8](#8-idempotency-what-id-and-reference-mean).
The response is deliberately opaque: it never says what Mycelium decided to
do with the event, because the only credential on this endpoint is a shared
secret.

**A 200 means custody, not an invoice.** The handler verifies, persists and
answers; the fiscal work happens in a worker shortly afterwards (polling
every `payment_connector_poll_interval_seconds`, 10 s by default). Anything
that can go wrong *after* acceptance -- a counterpart with no VAT number, a
credit note whose parent does not exist -- is reported on the event in the
connector's event list, never in the HTTP response.

## 2. Authentication

Two factors; the first is mandatory, the second is optional and configured
per connector.

### 2.1 The signature (mandatory)

| Header | Value |
| --- | --- |
| `X-Mycelium-Timestamp` | Unix time in **seconds**, as a decimal string |
| `X-Mycelium-Signature` | `v1=<hex>` |

```
signature = HMAC-SHA256(signing_secret, "{timestamp}.{raw_body}")   # hex
```

Rules that matter:

- The MAC covers the **exact bytes you transmit**, concatenated after the
  timestamp and a literal `.`. Sign the serialized body, not a
  re-serialization of it: any change in whitespace or key order produces a
  different MAC.
- `{timestamp}` in the MAC and the value of `X-Mycelium-Timestamp` must be
  the same string.
- The timestamp is checked against a **±300 s** window
  (`payment_connector_tolerance_seconds`). The window is symmetric: a
  far-future timestamp is refused too, or a captured request would stay
  replayable forever.
- `X-Mycelium-Signature` accepts `v1=<hex>`, a bare hex digest, and a
  comma-separated list of either (send both while you rotate your own
  signing code). Header names are case-insensitive.
- The connector's **previous** signing secret keeps verifying for a grace
  window after a rotation (`payment_connector_secret_grace_hours`, 24 h by
  default), so a rotation never drops an event already queued in your retry
  buffer.

The signing secret is shown **once**, when the connector is created or the
secret is rotated. On this contract Mycelium is the authority, so it can mint
one (it looks like `whsec_…`) — or you can supply a key the sender already
uses, at creation or at rotation; a supplied key must be at least 16
characters, since it is the entire authority of a public unauthenticated
endpoint. Either way it is stored encrypted (verifying a MAC requires
recovering it), never echoed by a read route, and the SPA keeps it masked
behind an explicit reveal so it is not left on screen.

For a vendor provider the choice does not exist: the secret is issued there and
Mycelium refuses to mint one, at creation and at rotation alike.

This is the same construction as Mycelium's *outbound* webhook signature
(ADR-0047) and as Stripe's: implement one and you have implemented all
three.

### 2.2 The ingress API key (optional)

If the connector was created with an API key, every request must also carry

```
X-Connector-Api-Key: mycelium_pc_…
```

The key is shown once at mint/rotate and stored as a one-way hash. Like the
signing secret, the previous key keeps working for the grace window. A
connector with no key configured ignores the header.

A request that fails **either** factor gets the same `401` with the same
body: the endpoint does not tell a caller which factor failed, nor whether
a key is required at all.

## 3. The envelope

Every event has the same four top-level fields:

```json
{
  "id": "evt_01J8ZQ1B7X00",
  "type": "invoice.payment",
  "created": 1786615200,
  "data": {"reference": "ORD-2026-0001"}
}
```

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | **yes** | Your unique id for this event. The deduplication key. **Max 255 characters** -- a longer one is refused with `400 payment_connector.payload_invalid`. |
| `type` | string | **yes** | One of `invoice.issue`, `invoice.credit`, `invoice.payment`. **Max 80 characters.** |
| `created` | integer | no | Unix seconds; recorded as the event's `occurred_at`. A non-integer is ignored. |
| `data` | object | **yes** | The event body; its shape depends on `type`. Must be present and non-empty, even for a type you expect to be ignored -- an absent or empty `data` parks the event. |

`id` and `type` are the only fields validated while your request is open:
missing either one is a `400`. Everything else -- including a missing
`data`, a missing `reference` or an unusable amount -- is validated when the
event is processed, and a failure there parks the event for an operator
instead of answering your call with an error.

An unrecognised `type` (with a `data` object present) is recorded and
marked `ignored`, so a typo shows up in the connector's event list rather
than silently doing nothing.

## 4. `invoice.issue` -- emit an invoice

Emits one TD01 for money you have collected, and (unless the connector says
otherwise) transmits it to SdI.

```json
{
  "id": "evt_01J8ZQ4M7YQ2",
  "type": "invoice.issue",
  "created": 1786615200,
  "data": {
    "reference": "ORD-2026-0001",
    "currency": "EUR",
    "customer_reference": "cus_9f3a71",
    "purpose": "Canone di manutenzione, agosto 2026",
    "paid": true,
    "customer": {
      "legal_name": "Hahn-Banach SRL",
      "country_code": "IT",
      "vat_number": "01234567890",
      "tax_code": "01234567890",
      "address": "Via Roma",
      "civic_number": "1",
      "postal_code": "00100",
      "city": "Roma",
      "province": "RM",
      "country": "IT",
      "sdi_code": "ABC1234",
      "pec": "amministrazione@pec.example.test",
      "email": "amministrazione@example.test"
    },
    "lines": [
      {
        "description": "Canone di manutenzione",
        "quantity": "1",
        "unit_price": "100.00",
        "vat_rate": "22"
      },
      {
        "description": "Ore extra",
        "quantity": "2",
        "unit_price": "50.00",
        "vat_rate": "22",
        "price_includes_vat": false
      }
    ]
  }
}
```

### 4.1 The `data` object

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `reference` | string | **yes** | Your id **for the money**, not for the event. Claimed as object key `("invoice", reference)`; see [§8](#8-idempotency-what-id-and-reference-mean). |
| `lines` | array | **yes** | At least one line. |
| `customer` | object | in practice | Omitted, it yields a counterpart named `Cliente` with no fiscal data, which is quarantined. |
| `currency` | string | no | Default `EUR`, upper-cased. Recorded on the event; the emitted document is in the issuer profile's currency and **no conversion is performed**. |
| `customer_reference` | string | no | Your stable customer id. Keys the connector's identity map -- see [§4.3](#43-customer-identity). |
| `purpose` | string | no | The document's causale. Falls back to the connector's `default_purpose`. |
| `paid` | boolean | no | Default `true`. `true` marks the emitted document paid (if the connector's `payment_sync_enabled` is on and the document is not left as a draft). |

### 4.2 `customer`

Every field is optional at parse time; what is *fiscally* required is in
[§4.4](#44-what-gets-quarantined).

| Field | Notes |
| --- | --- |
| `legal_name` | Ragione sociale. Used as the client's name. |
| `first_name`, `last_name` | Persona fisica. Used to build the name when `legal_name` is absent. |
| `country_code` | ISO-2. Falls back to the connector's `default_country_code`. |
| `vat_number` | Partita IVA. A country-prefixed value (`IT01234567890`) is normalised downstream. |
| `tax_code` | Codice fiscale. |
| `address`, `civic_number` | Indirizzo / numero civico. |
| `postal_code`, `city`, `province` | CAP, comune, sigla provincia (2 letters). |
| `country` | Falls back to `country_code`. |
| `sdi_code` | Codice destinatario, exactly 7 characters. **`0000000` is not accepted as a way to make a document emittable**: it cannot be used to deliver. Send the recipient's real code, or send `pec` instead. A counterpart outside Italy needs neither -- the connector applies the standard's `XXXXXXX` by rule. |
| `pec` | The counterpart's PEC. |
> The Stripe adapter reads the codice destinatario from the customer's **metadata** only. It deliberately does not interpret `address.line2`, where customers sometimes type it: a second address line is an address line, and guessing otherwise would decide invoice delivery from a street field.
| `email` | Ordinary email; carried for reference, not a FatturaPA field. |

The name is resolved as: `legal_name`, else `"{first_name} {last_name}"`,
else the literal `Cliente` (which will not survive the fiscal checks).

### 4.3 Customer identity

- With `customer_reference`: the first event for that value resolves or
  creates a client and **links** it to the reference. Every later event
  carrying the same `customer_reference` reuses that client directly, and
  the `customer` block in those later events is **not** re-read. Change a
  counterpart's fiscal data in Mycelium, not by re-sending it here.
- Without `customer_reference`: the client is resolved on fiscal identity
  (VAT / tax code) on every event, which is correct but does not protect
  you from creating a near-duplicate client if you send inconsistent
  spellings.

Sending a stable `customer_reference` is strongly recommended: it is also
what makes concurrent redeliveries of the same customer race-safe.

### 4.4 What gets parked, and where

Before composing anything, the connector requires the counterpart to carry:

- `vat_number` **or** `tax_code`;
- `sdi_code` **or** `pec` -- a real one. There is no connector-wide default,
  because `0000000` cannot be used to deliver. A counterpart whose country is
  not `IT` is exempt: the standard prescribes `XXXXXXX` and the connector
  applies it;
- `address`;
- `postal_code`;
- `city`.

If any is missing the event is parked in **`no_billing_data`** with the slug
`client_billing_data_missing` and the list of what was absent, and nothing is
emitted.

`no_billing_data` is NOT the operator queue. It is a waiting room whose
blocker is the customer, and it empties by itself: send an `invoice.issue`
later with the completed `customer` block, or -- on the Stripe adapter -- let
a `customer.updated` carry the data, and every payment waiting on that
customer re-arms automatically with a fresh attempt budget. You do not have to
re-send the payment event.

Emitting anyway with a generic recipient code is refused rather than offered:
an invoice issued to a business as if it were B2C is a wrong fiscal document,
and a wrong document can only be corrected by a second one (a TD04 credit
note).

Data that is present but unusable is parked the same way, under the slug
`client_rejected`: an Italian `vat_number` that is not exactly 11 digits,
for instance. Tax codes, VAT numbers and provinces are upper-cased and
trimmed before the client is looked up, so casing and stray spaces do not
create a second client for the same counterpart.

### 4.5 Lines

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `unit_price` | decimal **string** | **yes** | Price **per unit**. See [§7](#7-money-is-a-decimal-string). |
| `quantity` | decimal string | no | Default `1`. A value `<= 0` is treated as `1`. |
| `description` | string | no | Falls back to the connector's `default_line_description`, then to `Servizio`. Truncated at 1000 characters. |
| `vat_rate` | decimal string | no | A **percentage**: `"22"` means 22%. Falls back to the connector's `default_vat_rate`, then to the issuer's regime default. |
| `vat_nature` | string | no | FatturaPA `Natura` (e.g. `N2.2`) for a line with no VAT. Falls back to the connector's `default_vat_nature`. |
| `price_includes_vat` | boolean | no | Whether `unit_price` already contains the VAT. **State it** if you know: it is believed. Omitted, the connector's `vat_pricing` decides, and under its default (`auto`) an unstated price is read as VAT-**inclusive** — the amount is money already collected, so adding VAT on top would invoice more than was paid. |

`price_includes_vat` decides how `unit_price` is read:

- `false` (the usual case): `unit_price` is **net**, VAT is added on top.
- `true`: `unit_price` is **gross**; the net is derived as
  `unit_price / (1 + vat_rate/100)`, rounded to 4 decimals. With no
  resolvable rate the figure is passed through unchanged rather than split
  on a guessed rate.

FatturaPA has one aliquota per line: a genuinely multi-rate item must be
sent as several lines.

## 5. `invoice.credit` -- reverse an invoice

Emits a TD04 credit note against a document previously emitted through this
connector.

```json
{
  "id": "evt_01J8ZQ9RRB44",
  "type": "invoice.credit",
  "created": 1786701600,
  "data": {
    "reference": "REF-2026-0001",
    "parent_reference": "ORD-2026-0001",
    "amount": "61.00",
    "currency": "EUR",
    "reason": "Reso parziale ordine ORD-2026-0001"
  }
}
```

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `reference` | string | **yes** | Your id for the credit. Claimed as `("credit_note", reference)` -- its own namespace, so it may repeat an invoice reference, though unique values are easier to reason about. |
| `parent_reference` | string | **yes** | The `reference` of the `invoice.issue` being reversed. |
| `amount` | decimal string | no | **Gross** amount refunded, major units. Absent (or `>=` the parent total) = full reversal. |
| `lines` | array | no | Same shape as `invoice.issue` lines. When present they **replace** the copied parent lines entirely. |
| `currency` | string | no | Default `EUR`. As on `invoice.issue`, informational. |
| `reason` | string | no | Becomes the credit note's causale. |

How the amount is applied:

- **Full reversal** (no `amount`): the TD04 is a verbatim copy of the
  parent's lines.
- **Explicit `lines`**: exact. The copy is discarded and your lines are
  used, each with its own rate. Prefer this when the refund is not a flat
  proportion of a multi-rate invoice.
- **Bare `amount`**: the copied lines are scaled down pro-rata with
  largest-remainder allocation, which preserves each line's aliquota and
  sums exactly to the target net. On a multi-rate invoice the recomputed
  VAT can land a cent away from the gross you refunded; send `lines` if
  that cent matters.

Ordering and parent state:

- If the parent has not been emitted yet, the event is **retried with
  backoff** (`parent_not_emitted`) -- event ordering is not assumed -- and is
  parked only once the attempt budget runs out.
- A parent still in `draft` parks the event (`parent_not_transmitted`); a
  parent rejected by SdI parks it (`parent_rejected`), because a scartato
  invoice was never validly issued and its correction is a resend, an
  operator decision.

## 6. `invoice.payment` -- mark an invoice paid

Carries no fiscal content: it reconciles payment state for a document that
already exists.

```json
{
  "id": "evt_01J8ZQC1TT08",
  "type": "invoice.payment",
  "created": 1786705200,
  "data": {
    "reference": "ORD-2026-0001"
  }
}
```

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `reference` | string | **yes** | The `reference` of the `invoice.issue` whose document should be marked paid. |

- A reference Mycelium has no document for is **not an error**: the event is
  marked `ignored` (it is money we did not invoice -- a payment predating the
  connector, for instance).
- The document is marked paid only if the connector's `payment_sync_enabled`
  is on and the document is past `draft`.

## 7. Money is a decimal string

**Every monetary value in this contract is a JSON string.** `"12.34"`,
never `12.34`. The same holds for `quantity` and `vat_rate`.

Why: a JSON number is a float in almost every parser, and a float has
already lost the exactness a fiscal document needs by the time it reaches
us -- `0.1 + 0.2` is not `0.3`, and this number ends up in an XML that SdI
re-checks arithmetically. Refusing floats rather than rounding them means a
sender's precision bug cannot become our filing.

Integers are accepted where they are exact (`"quantity": 3`, `"unit_price":
10`), and are read as exact decimals.

Every decimal field is enforced the same way: `unit_price`, `quantity`,
`vat_rate` and the credit `amount` all reject a JSON number, and the event is
parked as `payload_invalid` with the offending field named. The distinction the
parser makes is between *absent* (falls back to the documented default) and
*present but not a decimal string* (a hard refusal) -- collapsing those two is
exactly how a float `quantity` of `2.5` could once become a quantity of `1`,
and a partial credit `amount` could become a full reversal, silently. Serialize
amounts as strings.

## 8. Idempotency: what `id` and `reference` mean

Two different keys, two different jobs.

**`id` is the delivery dedup key.** The first event with a given `id` on a
given connector is stored; any later one collapses onto it
(`UNIQUE (connector_id, provider_event_id)` plus
`INSERT … ON CONFLICT DO NOTHING`) and answers `200` with
`duplicate: true`. Retry as hard as you like: the same `id` cannot be
processed twice. A **new** `id` is a **new** event.

**`reference` identifies the money.** It is claimed against the document
Mycelium emits, and it is what a later credit or payment event points at:

- `invoice.issue` claims `("invoice", reference)`;
- `invoice.credit` claims `("credit_note", reference)` and **resolves its
  parent** through `("invoice", parent_reference)`;
- `invoice.payment` resolves through `("invoice", reference)`.

Consequences worth internalising:

- Two events with **different `id`s but the same `reference`** do not
  produce two invoices. The second resolves to the document the first
  emitted (and, if it says `paid`, marks it paid). This is the guarantee
  that a lost acknowledgement on your side cannot burn a second fiscal
  number.
- Therefore, **never reuse a `reference` for genuinely different money**.
  It will silently attach to the earlier document and the second sale will
  have no invoice.
- The claim is committed **before** the document is transmitted to SdI, so a
  crash mid-filing resumes that document instead of composing a new one.

## 9. Status codes and retries

| Status | Body `code` | Meaning | What a sender should do |
| --- | --- | --- | --- |
| `200` | -- | Accepted, or a duplicate of an event already accepted. | Done. |
| `400` | `payment_connector.payload_invalid` | Not JSON, not an object, missing `id`/`type`, or over the size limit. | Fix and resend, reusing the same `id` (no event was stored; only the refused delivery was logged). Do not retry unchanged. |
| `401` | `payment_connector.signature_invalid` | Bad or expired signature, **or** a bad/missing ingress API key. The two are deliberately indistinguishable. | Check the secret, the timestamp skew, and the key. Do not retry unchanged. |
| `403` | `payment_connector.disabled` | Signature valid, connector switched off by its owner. | Retry with backoff; it will succeed when the owner re-enables it. |
| `404` | `payment_connector.not_found` | Unknown or revoked connector, wrong `provider` segment, or the subsystem is disabled in this deployment. | Stop and reconfigure. |
| `5xx` | -- | Unexpected server-side failure. | Retry with backoff. Safe: `id` deduplicates. |

Error bodies are `{"code": "...", "detail": "..."}`. `detail` is localized
prose for humans; branch on `code`.

Every request against a connector that resolves -- accepted **and**
refused -- is appended to the connector's delivery ledger with its outcome,
the body's SHA-256 digest, its size, and whether a signature and a key were
present. The body itself is not stored. So "we sent it and there is no
invoice" is answerable from the database rather than from a log: the
workspace UI surfaces the refused deliveries, and the ledger route serves
the full list. If a delivery is in neither, it never reached a resolved
connector -- check the URL and the `connector_id`.

## 10. Worked example: computing a signature

Secret `whsec_test_secret`, timestamp `1786615200`, and this body sent
**exactly** as these 101 bytes:

```
{"id":"evt_pay_1","type":"invoice.payment","created":1786615200,"data":{"reference":"ORD-2026-0001"}}
```

The signed string is the timestamp, a dot, then the body:

```
1786615200.{"id":"evt_pay_1","type":"invoice.payment","created":1786615200,"data":{"reference":"ORD-2026-0001"}}
```

which gives

```
HMAC-SHA256 = f265dbf26d34e56f6acc588d98a4912283969c95fd24d150e50198a80630cd20
```

In Python:

```python
import hashlib, hmac, json, time
import httpx

secret = "whsec_test_secret"
event = {
    "id": "evt_pay_1",
    "type": "invoice.payment",
    "created": 1786615200,
    "data": {"reference": "ORD-2026-0001"},
}
# Serialize ONCE, sign and send the same bytes.
body = json.dumps(event, separators=(",", ":")).encode()
ts = str(int(time.time()))
sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()

httpx.post(
    f"{base}/api/v1/connectors/mycelium/{connector_id}",
    content=body,
    headers={
        "Content-Type": "application/json",
        "X-Mycelium-Timestamp": ts,
        "X-Mycelium-Signature": f"v1={sig}",
        # only when the connector requires one
        "X-Connector-Api-Key": api_key,
    },
)
```

In shell:

```
printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -hex
```

Reproduce the constant above with `TS=1786615200`, `SECRET=whsec_test_secret`
and `BODY` set to the one-line JSON above -- if your implementation prints
`f265db…`, it will verify.

## 11. What the connector does with your events

The behaviour of an accepted event depends on the connector's own switches,
which are the workspace owner's decision, not the sender's:

| Setting | Values | Effect |
| --- | --- | --- |
| `enabled` | on/off | Off = signed requests are refused with `403`, and events already queued when it was switched off end as `ignored`. |
| `invoice_mode` | `transmit` / `draft` / `off` | File with SdI / compose and stop for review / record the event and park it for a manual decision. |
| `credit_note_mode` | `transmit` / `draft` / `off` | The same, independently, for `invoice.credit`. |
| `payment_sync_enabled` | on/off | Whether `paid` and `invoice.payment` mark documents paid. |
| `series`, `default_*` | -- | Sezionale and the fiscal defaults that fill in what your events leave out. |

Event lifecycle, visible in the connector's event list:

| Status | Meaning |
| --- | --- |
| `pending` | Due for a processing attempt. |
| `processing` | A worker holds the lease. |
| `done` | Produced its document, or was a settled no-op. |
| `ignored` | Recognised but not actionable under this configuration (unknown type, payment for money we did not invoice). |
| `done` | Produced its document. In shadow mode (`dry_run`) that document was composed and validated but never sent; its XML is downloadable from the connector. |
| `no_billing_data` | **Waiting room**: the counterpart has not supplied a complete billing block. Nobody has to act -- it re-arms itself when the data arrives. |
| `needs_attention` | **Operator queue**: a decision is pending (manual mode, malformed body, a parent that will never settle). Re-runnable from the UI once resolved. |
| `dead` | Exhausted its attempts (`payment_connector_max_attempts`, 10 by default, with exponential backoff). |

`no_billing_data`, `needs_attention` and `dead` rows are never swept by
retention.

### Shadow mode

A connector may be configured to accept your events, compose the documents
and validate them against the official FatturaPA schema **without sending
anything** -- so an integration can be verified against a live one before it
takes over. From the sender's side nothing changes: the endpoint, the
signature and the responses are identical, and a `200` still means the event
was accepted. The difference is only in what the receiving org does with it.
Refunds (`invoice.credit`) are not shadowed: a credit note corrects an
emitted document, and in shadow mode nothing is emitted.
