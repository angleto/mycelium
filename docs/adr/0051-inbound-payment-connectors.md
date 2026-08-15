# ADR-0051 Inbound payment connectors (provider webhooks -> FatturaPA)

Status: accepted (migration 0092; builds on ADR-0011 intermediary/mandate,
ADR-0045 issuer-scoped keys, ADR-0046 two-phase durable transmit, ADR-0047
signed outbound webhooks).

## Context

Money is collected in a payment provider (Stripe, a checkout, a
subscription); the fiscal document for that money is then composed by hand
in Mycelium. The two systems already agree on everything a FatturaPA needs
except the fiscal identity of the counterpart, and the manual step is where
invoices go missing, get issued late, or get issued twice.

The obvious market answer is a **third-party e-invoicing intermediary**:
plug the provider into a SaaS that speaks SdI, and let it file. ADR-0011
already examined and rejected exactly that option -- "Third-party
intermediary via API: valid but rejected because the user wants no third
party seeing the invoices; the mandate model with our own channel satisfies
the constraint while staying legally correct." That rejection is the
doctrinal basis of this ADR: we already hold the mandate, the channel, the
numbering and the conservation status, so the only thing missing between a
payment and a filed invoice is a way to hear about the payment. Buying that
piece from a third party would hand it precisely the data ADR-0011 refused
to hand over -- the full invoice, the counterpart, and the amounts.

**The tension this ADR has to own.** ADR-0026 states that Mycelium is
vendor-neutral and that the LLM glue lives outside; its own words are that
the stance "is about not embedding *other vendors'* connectors in Mycelium".
An in-repo Stripe adapter **is** another vendor's connector, and pretending
otherwise would be dishonest. The carve-out we actually made is narrower
than "Stripe is fine now":

- the engine speaks a **neutral event vocabulary** (`payment_events`: a
  counterpart, lines, an emission/credit/payment intent, a set of provider
  object keys). Nothing downstream of the mapper -- client resolution, draft
  composition, credit notes, transmission -- knows what Stripe is;
- we ship a **published contract of our own**, provider `mycelium`
  ([payment-connector-contract.md](../payment-connector-contract.md)), which
  is a thin JSON projection of those same DTOs. A sender we have no adapter
  for integrates by implementing a documented format instead of waiting for
  us to write their integration;
- Stripe is therefore the **first adapter over an open boundary**, not the
  boundary itself. Deleting `payment_stripe.py` would remove a convenience,
  not the feature.

This is the same shape as `SdiChannel` / `EmailConnector` / `Embedder`
(ADR-0012, ADR-0023): a Protocol, a registry keyed on a column, neutral
DTOs. What is new is only that one of the shipped implementations carries a
vendor's name, and that is a cost we accept with our eyes open, not a
principle we quietly dropped.

The hard constraint is that this bolts onto a **live fiscal system**. An
emission allocates a progressive number and files a document with SdI
(ADR-0046). A double filing here is not a duplicated row: it is a second
fiscal document for one sale, correctable only by a TD04 credit note, with a
number burned in between. Provider webhooks are at-least-once, unordered,
retried on any non-2xx, and time out in seconds.

## Decision

A per-issuer-profile **payment connector** whose ingress is a public
unauthenticated webhook and whose fiscal work happens in a worker.

1. **Neutral in, fiscal out.** A `PaymentEventMapper` per provider
   (registry keyed on `payment_connectors.provider`) parses bytes and dicts
   and returns intents. It performs no I/O, touches no session and knows
   nothing about invoices; every fiscal decision lives in one place
   (`services/payment_connectors.py`). Adding a provider is one module plus
   one CHECK widening, and touches no fiscal code. Two implementations
   ship: `stripe` and `mycelium` (the published contract).

2. **The ingress is asynchronous.** The HTTP handler resolves the tenant,
   verifies a MAC over the raw body, verifies the optional ingress key,
   parses, INSERTs the event and answers 2xx. It does no fiscal work at all.

   This does **not** contradict ADR-0046's rejection of an outbox worker.
   That rejection was about the *outbound* verb: an outbox would flip the
   **synchronous transmit contract** that the SPA, `/api/v1` and MCP all
   depend on -- all three return the SdI result to a caller who is waiting
   for it. Here nobody is waiting: a provider webhook has no user behind
   it, and its own retry policy reads our status code. The numbers make the
   inversion mandatory rather than merely convenient: an SdI dispatch is
   allowed `sdi_dispatch_timeout_seconds = 120`, which exceeds every
   provider's webhook timeout, so a synchronous emission would be reported
   **failed and redelivered while the first attempt was still filing** --
   the exact double-filing this subsystem exists to prevent. The existing
   synchronous verbs are untouched; this adds a second, differently shaped
   entry point beside them, it does not replace one.

3. **No double filing.** Four mechanisms, each covering a different
   failure:

   - `UNIQUE (connector_id, provider_event_id)` with `INSERT ... ON CONFLICT
     DO NOTHING` is the whole at-least-once story on the way in. A provider
     redelivery, a client-side retry and two API replicas racing the same
     POST collapse onto one row, and the sender gets 2xx either way (a
     redelivery IS a success from its point of view).
   - `payment_object_links` claims **every** provider id that names this
     money (invoice, payment intent, charge, checkout session) against the
     document we emitted, and the claim is written and **COMMITTED before
     the SdI dispatch**. A crash inside the two-phase transmit therefore
     finds the link on retry and RESUMES that document's transmission;
     without the claim the retry would compose a second draft and burn a
     second fiscal number for one payment. The link's FK to the invoice is
     RESTRICT, not SET NULL: a dangling claim would silently re-open the
     door it exists to close.
   - the worker **commits the claim in a transaction separate from the
     work**. This is the one place the loop deliberately differs from the
     ADR-0047 outbound deliverer, which holds its transaction across the
     network call so its `delivering` marker never actually commits. Here a
     rewound claim would let a second worker start the same emission, so
     `claim_due` (`FOR UPDATE SKIP LOCKED`) commits, and only then does
     each event run in its own transaction. An uncommitted claim is not a
     lease, it is a lock that disappears on crash.
   - the lease is **600 s** (`payment_connector_lease_seconds`), chosen to
     exceed `sdi_dispatch_timeout_seconds` (120) + `sdi_dispatch_lease_seconds`
     (300). That inequality is what makes reclaiming safe: an expired lease
     provably means no dispatch is still in flight for that event, so the
     retry cannot race a filing that is still happening.

4. **Credit notes are provider-driven, and order-independent in the count
   of documents.** Stripe models one reversal twice: `credit_note.created`
   (the document) and `charge.refunded` / `refund.created` (the money);
   refunding from the dashboard fires both, in no guaranteed order.
   `credit_note.created` is the richer signal and takes precedence in what
   it produces -- it carries explicit lines with the exact per-rate split,
   so the TD04 is exact. `charge.refunded` is the fallback for a refund
   issued without a credit note, and drives a pro-rata reduction of the
   copied parent lines. Rather than pick one and lose the other, **both are
   mapped and both claim the shared refund id**, so whichever arrives first
   files the TD04 and the other resolves to it. The residual, stated
   plainly: delivery order does not change *how many* documents exist (the
   invariant that matters), but it can change their *line detail* -- if
   `charge.refunded` wins the race, the note is the pro-rata one even
   though a `credit_note.created` was in flight behind it. A refund whose
   parent has not been emitted yet is retried, not quarantined: provider
   ordering is not guaranteed, and the attempt budget parks a genuinely
   orphan refund on its own.

5. **Automation is a per-connector switch, not an always-on behaviour.**
   `invoice_mode` and `credit_note_mode` are independently `transmit` /
   `draft` / `dry_run` / `off`.

   `dry_run` (migration 0093) is the shadow mode, and it exists because none
   of the other three answers the question an operator actually has before
   cutting over from an incumbent provider: *would the documents this thing
   produces be correct?* `draft` composes but never builds an XML -- the
   document is frozen at transmit (ADR-0046) -- so there is nothing to inspect
   and nothing to diff; `transmit` answers the question by filing real
   documents, which is the risk being avoided. `dry_run` runs the entire real
   path -- counterpart resolution into the anagrafica, lines, totals, the
   FatturaPA build, the official XSD validation -- and stops one step before
   SdI, freezing the generated XML on the event so it can be downloaded and
   compared against what the incumbent filed for the same payment.

   Three properties make it genuinely reversible rather than a half-measure:

   - it allocates NO fiscal number. The XML carries the would-be number and
     the `ANTEPRIMA` progressivo that `get_xml_preview` already produces, so
     shadowing can never consume a sequence a real document will need;
   - its object claims live in their own key namespace (`dryrun:`). The
     claims must exist, or a redelivery during the shadow period would
     produce a second shadow document; but they must be invisible to a later
     live run, or switching the flag off would find the shadow draft, resume
     it, and file a document composed from data the shadow period existed to
     distrust;
   - the shadow document is ARCHIVED, out of the list an operator browses to
     transmit from, while staying fully inspectable. The marking is
     deliberately NOT written into `purpose` or `notes`: both become
     `Causale` in the XML, so labelling the document there would contaminate
     the exact artefact the shadow run exists to verify.

   Credit notes are not shadowed. A TD04 corrects an EMITTED document
   (ADR-0009) and in shadow mode nothing ever is, so `credit_note_mode =
   dry_run` parks the event saying so rather than validating a fiction --
   during a parallel run the incumbent is still issuing the storni anyway.

   A shadow document that fails validation parks immediately with the
   validator's own words instead of being retried: that finding IS the output
   of a shadow run, and a condition that cannot self-resolve should not be
   buried under attempts. Automating emission while keeping storni manual is a
   legitimate and common posture, and an operator who wants the connector
   to compose but not file (a review step before a fiscal number is spent)
   must not have to choose between "all of it" and "none of it". Both
   default to `transmit`, but a connector is created `enabled = false`:
   nothing is filed until an owner switches it on.

6. **The counterpart's fiscal identity comes from the provider's CUSTOMER
   events, and lands in the org's own anagrafica.** A Stripe webhook payload
   **cannot be expanded**, and an invoice event names its customer by id
   alone -- measured on a live account, 1212 of 1212 `invoice.paid` events
   carried `customer` as a bare string. So the VAT number, codice
   destinatario and PEC an integrator stores on the Stripe customer never
   travel with the invoice that needs them. They arrive on a different
   event, at a different time, and the connector has to remember them.

   It remembers them in `client_profile` -- the record the FatturaPA is
   built from, that an operator can edit, that already carries RLS,
   versioning, audit and revisions -- and **not** in a connector-owned copy.
   A second copy would be a second truth, and the first time the two
   disagreed the document would be built from whichever one the code
   happened to read. `payment_customer_links` therefore holds no fiscal
   data at all: it is a pure identity map from an external id to an
   internal one, and its UNIQUE is what makes concurrent redeliveries of one
   payment resolve to one client.

   The measurement that made the cache unnecessary: of 649 provider
   customers, 148 carried fiscal data and only **5** of those had never been
   invoiced. Registering a client as soon as its fiscal identity is known
   costs five extra rows, not a polluted directory.

   The write goes through `taxonomy.fill_client_gaps`, which can only fill
   an EMPTY field and never overwrite. That is what makes it safe to run
   unattended on every customer event: a corrected address or a
   hand-entered codice destinatario always outlives whatever the provider
   says afterwards. It is deliberately a separate door from
   `update_client`, and MEMBER level rather than admin, precisely because
   it cannot destroy anything -- a webhook must not hold overwrite power.

   Provider metadata keys are an **ordered candidate list** per field, not
   one name. Real accounts accumulate spellings: the account this was
   measured against carries `vatId`, `fiscal_code`, `codice_destinatario`
   and `pec` alongside a dead vendor's `billit_identifier_*` keys and a
   capitalised `Codice Destinatario` typed in by hand. First key present
   wins, so the list is also the precedence order.

7. **Two parked states, because two different people have to move.**
   Before composing, the runner asserts the counterpart data
   `invoice._validate` will demand at transmit: a VAT number or a tax code,
   a codice destinatario or a PEC, address, CAP, city.

   - `no_billing_data` -- the customer never supplied a complete billing
     block. Nothing is broken and there is nothing for an operator to
     decide. These **re-arm themselves**: when a customer event arrives
     carrying the missing fiscal identity, every payment waiting on that
     customer returns to `pending` with a fresh budget and the invoice is
     emitted with nobody pressing anything. For a monthly subscription
     bought before the customer completed their details, the alternative is
     noticing by hand every month.
   - `needs_attention` -- a genuine decision is pending (automation switched
     to manual, a scartato parent, an unparseable payload). A human must act.

   Collapsing them, as the first revision did, buries the handful of events
   that need a person under the many that only need a customer to get round
   to it, which is how an operational queue stops being read.

   **There is no default codice destinatario.** `0000000` is the FatturaPA
   value for "no channel, leave it in the fiscal drawer", but it cannot be
   used to actually deliver, so a connector-wide default for it would only
   make an invoice LOOK emittable while producing a document that goes
   nowhere. A recipient is addressable when the counterpart supplied a real
   code or a PEC. The single exception is not a default but a rule: a
   counterpart outside Italy has no Italian recipient code by construction,
   and the standard prescribes `XXXXXXX`, which the connector derives from
   the country (49 of the 1212 measured invoices).

   The tempting alternative, emitting to a private individual with a
   generic code whenever the fiscal metadata is absent, is refused: an
   invoice issued to a business as B2C is a **wrong document**, correctable
   only by a TD04 (a second document, a second number, an explanation to
   the client). A parked event costs an operator one minute. The asymmetry
   is not close.

   Replaying the 2817 real events of a live account through the shipped
   mapper: 678 of 1212 payments (56%) emit under these rules, and not one
   payload was rejected by the parser. Of the rest, 106 lack **only** a
   codice destinatario -- everything else present. That number is a
   business signal, not a defect: it names exactly which customers to ask.

8. **The delivery ledger stores the body's SHA-256, not the body.** Every
   inbound request against a resolved connector appends a row -- refusals
   included -- because "the provider says it delivered this and there is no
   invoice" has to be answerable from the database rather than from
   whichever pod's log survived. The bytes themselves are not kept: for an
   accepted event the frozen payload is already on the event row, so
   storing it twice buys nothing; for a **refused** one the bytes are
   unauthenticated attacker-controlled data that we would be persisting at
   the attacker's request, in the very table that exists to audit them. The
   digest keeps the row verifiable -- anyone holding the original body can
   prove it produced this row -- at none of that cost. Requests for a
   connector id that does not resolve are deliberately not recorded at all:
   there is no tenant to attribute them to.

9. **Authentication is a MAC, not the URL.** The connector id in the path
   is a routing selector; authority comes from HMAC-SHA256 over
   `{timestamp}.{raw_body}` under a per-connector signing secret, verified
   over the RAW bytes before any parsing (a body normalised by `json.loads`
   no longer hashes to what the sender signed, and parsing first would hand
   an unauthenticated caller our parser). The construction is byte-identical
   to the ADR-0047 outbound signer and to Stripe's, so the repo has exactly
   one webhook MAC to audit in both directions. An optional second factor
   (`X-Connector-Api-Key`) is a one-way peppered hash; the signing secret is
   a reversible Fernet envelope, because a MAC we did not generate has to be
   recomputed. Both rotate with a grace window so a rotation never drops a
   redelivery already queued at the provider. Tenancy is resolved with no
   `app.current_org` in scope through a SECURITY DEFINER function, the same
   shape ADR-0045 used for issuer keys; the actual writes re-enter through
   `tenant_session` and are RLS-scoped like everything else.

10. **Statelessness is preserved.** `docs/non-functional-requirements.md`
   already requires stateless services (arm64, 12-factor, scalable workers)
   and lists the known stateful exceptions: Postgres, the Proton Bridge
   sidecar, the inbound SdI SOAP endpoint. **This subsystem adds no new
   stateful exception.** Authentication is a stateless MAC over the request
   body, deduplication is a UNIQUE constraint, the work queue is a table
   claimed with `FOR UPDATE SKIP LOCKED` under an expiring **column** lease
   (not a session-scoped advisory lock, rejected in ADR-0046 because it
   does not survive the pooled connection release), and the only advisory
   lock used is transaction-scoped. N API replicas and N workers are
   interchangeable, and a pod dying mid-emission loses no event.

## Consequences

- Arming is per issuer profile, not fleet-wide. `payment_connectors_enabled`
  ships ON and is an emergency lever (ingress 404s, worker loop unregistered,
  a worker restart to flip it either way); the fail-closed guarantee lives on
  the connector row, which is created `enabled = false` and cannot exist
  without the provider's own signing secret. A fleet flag on top of that adds
  no safety -- nothing is emitted by a connector nobody deliberately created
  and armed -- while being able to leave a correctly configured connector
  silently inert.
- A connector is a new **non-user principal**: migration 0092 widens the
  `activity_log` and `entity_revision` actor-kind CHECKs with
  `payment_connector`, so a document emitted by an integration is
  attributed to the integration rather than laundered through `system`.
  The downgrade re-narrows those CHECKs `NOT VALID` on purpose (both tables
  are append-only; an audit trail a downgrade can erase is not one).
- `payment_connectors` is ENABLE-but-NOT-FORCE RLS, the same deliberate
  asymmetry as `issuer_api_keys`: the SECURITY DEFINER resolver must read a
  row before any tenant context exists. Its four sibling tables are FORCE.
- A misconfigured connector spends real fiscal numbers. That is why
  `enabled` defaults to false, why the modes exist, and why the
  parked states are terminal rather than retried.
- A partial refund driven by a bare amount is allocated across the copied
  parent lines with largest-remainder, so the scaled amounts sum exactly to
  the target; the per-rate VAT is still recomputed by the invoice service,
  so a **multi-rate** partial can land a cent off the refunded gross. A
  provider that sends explicit credit-note lines bypasses that path and is
  exact.
- Provider-customer -> client deduplication is race-safe **within a
  connector** (a transaction advisory lock plus a UNIQUE link), which is
  the race at-least-once delivery actually creates. A client created
  concurrently in the SPA with the same VAT can still duplicate, because
  `client_profile` carries no uniqueness on fiscal identity: a pre-existing
  gap this subsystem does not widen and does not close.
- Retention: terminal events (`done`, `ignored`) and delivery rows are
  swept after `payment_connector_event_retention_days` (730 by default, and
  the delivery keeps its own clock so the evidence outlives the event).
  `no_billing_data`, `needs_attention` and `dead` are **never** swept: the
  first is a customer waiting room, the other two the operator's queue. The invoice is the durable fiscal record and is never
  touched by the sweep.
- Connector management is REST + GUI only, never MCP: minting the
  credential that lets an outside system emit fiscal documents in this
  org's name is the same chicken-and-egg carve-out that keeps issuer keys
  and agent tokens off the assistant surface. The triage list deliberately
  does not project the raw provider payload (it carries the counterpart's
  personal data).
- Config knobs: `payment_connectors_enabled`, `payment_connector_base_url`,
  `payment_connector_tolerance_seconds`, `payment_connector_max_body_bytes`,
  `payment_connector_poll_interval_seconds`,
  `payment_connector_lease_seconds`, `payment_connector_max_attempts`,
  `payment_connector_backoff_base_seconds` / `_cap_seconds`,
  `payment_connector_batch`, `payment_connector_secret_grace_hours`,
  `payment_connector_event_retention_days`. No new deployment secret: the
  envelope is keyed by `secret_key`, the ingress key peppered with
  `issuer_key_pepper`.

## Alternatives rejected

- **A third-party e-invoicing intermediary** plugged into the payment
  provider. Rejected on ADR-0011's grounds, unchanged: the user wants no
  third party seeing the invoices. We already hold the mandate and the
  channel; buying this piece would give away exactly what the mandate model
  was chosen to keep.
- **Emitting synchronously inside the webhook handler.** Simpler and
  strictly wrong: the SdI dispatch budget (120 s) exceeds every provider's
  webhook timeout, so the provider would report the delivery failed and
  redeliver while the first attempt was still filing.
- **Polling the provider's API instead of receiving webhooks.** Needs an
  outbound credential with read access to the whole payment account
  (strictly more authority than a signing secret that only verifies), adds
  a vendor SDK and a scheduled reconciliation, and still needs the same
  dedup ledger. The webhook already carries the money event, signed.
- **Taking the `stripe` SDK as a dependency.** Everything needed from it on
  this path is an HMAC over the raw body and some dictionary reading; the
  package would add an untyped import, a release cadence and a second HTTP
  client to a path that already has one.
- **Emitting on every payment-ish event type.** Stripe fires several events
  for one payment (an `invoice.paid` is also a `charge.succeeded` and a
  `payment_intent.succeeded`); a set of triggers would file three invoices
  for one sale. Exactly one type mints a document and the others are
  demoted to payment reconciliation.
- **A B2C fallback when the counterpart's fiscal data is missing.** An
  invoice to a business emitted as B2C is correctable only by a TD04: a
  wrong document is more expensive than a parked event.
- **Storing the raw refused body** in the delivery ledger, for
  "debuggability": it makes the audit table an attacker-controlled store.
  The digest answers the question that is actually asked ("is this the body
  you sent?").
- **A user-editable mapping DSL** in the database instead of typed adapters:
  it would put fiscal arithmetic behind an untested, untyped, per-tenant
  configuration surface. The configurable part is deliberately limited to
  the metadata KEY NAMES and the fiscal defaults.
- **A Redis queue** for the events instead of a table: the queue holds the
  provenance of fiscal documents, so it must be as durable and as
  tenant-scoped (RLS) as the documents themselves, and it must not add a
  second failure domain to the filing path.
- **A session-scoped advisory lock as the in-flight arbiter**: already
  rejected in ADR-0046 -- it does not survive the pooled connection release
  at commit. A column lease does.
