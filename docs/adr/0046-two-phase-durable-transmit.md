# ADR-0046 Two-phase durable invoice transmit (lost-ACK safety)

Status: accepted (task b6a0df8f; follow-up deferred from ADR-0045).

## Context

`transmit` used to allocate the invoice number, the per-trasmittente
`ProgressivoInvio`/`NomeFile`, freeze the XML, set `state=transmitted` and
then perform the SdI `RiceviFile` network call **inside the same request
transaction** (committed at FastAPI dependency teardown). If the channel
raised after SdI had received the file (lost ACK / timeout), the whole
transaction rolled back: the DB retained zero record that a file left,
including both counter increments. The consequences were worse than a
double-filing of one invoice: the rolled-back counters could hand the SAME
numero/progressivo/nome_file to a DIFFERENT document next, an identity
collision between distinct fiscal documents. The F8 reuse branch shipped
with ADR-0045 was unreachable (nothing ever committed on failure).

The known blockers, quoted in ADR-0045 at deferral time: the row `FOR
UPDATE` lock is held by the request transaction (a nested session committing
pre-dispatch deadlocks on it), and the tenant GUCs are
`set_config(is_local=true)` — transaction-local — so any mid-request commit
loses the RLS context. Empirically, SQLAlchemy also refuses statements after
a commit inside the `session.begin()` context manager.

## Decision

**In-request two-phase transmit on the same session**, no outbox worker.

1. `tenant_session` uses explicit begin/commit (autobegin, commit on clean
   exit, rollback on exception) instead of the `begin()` CM — semantics
   unchanged for every caller. A new `tenant_checkpoint(session)` runs the
   search-dirty flush hooks, captures the `app.*` GUCs (including the
   public-API `member` role pin), commits, and re-arms them as the first
   statement of the next transaction. A `tenant_rollback(session)` twin
   re-arms from values stashed at session open (an aborted transaction
   cannot be queried); the SdI-redelivery dedupe uses it.
2. **Phase 1 (prepare, committed pre-dispatch)**: lock the row
   (`FOR UPDATE` **with `populate_existing`** — an unlocked read may have
   cached a stale copy in the identity map, which would bypass the lease),
   validate, allocate numero + progressivo/nome_file (or reuse the burned
   ones), freeze the XML, set `state=transmitted`, stamp the dispatch lease
   `sdi_dispatch_started_at` and the active `sdi_env_used`, checkpoint.
   From here the DB always knows which file may exist at SdI.
3. **Phase 2 (dispatch)**: no lock, no open invoice transaction. The wall
   time is bounded by `asyncio.timeout(sdi_dispatch_timeout_seconds=120)`
   (httpx's 30 s timeout is per phase, not total), so an expired lease
   (`sdi_dispatch_lease_seconds=300`) provably implies no in-flight dispatch.
4. **Phase 3 (record)**:
   - success rides the caller's transaction (atomic with the public API's
     idempotency snapshot). The write is state-gated on a fresh locked
     re-read: if the inbound reconcile settled the invoice while dispatching,
     the reconciled state wins and only the lease is cleared.
   - failure self-commits via a checkpoint (it must survive the request
     rollback that follows the re-raised error) and is **classified**:
     *definitely-not-filed* (connection never established:
     `httpx.ConnectError`/`ConnectTimeout`; an explicit RiceviFile `Errore`:
     `SdiFileRejectedError`; a local mTLS-config failure:
     `SdiLocalConfigError`) **and the attempt started from draft** → back to
     draft, keeping numero/issued_at/progressivo/nome_file for verbatim
     reuse, clearing the XML and the lease. On a **retry** even a definite
     failure only ends the attempt (the earlier lost-ACK one may have filed;
     the frozen XML is the only copy of what it may have filed).
     *Ambiguous* (everything else) → parked: `transmitted`, ident-less,
     lease kept — retryable at lease expiry, error `409
     invoice.transmit_unconfirmed`.
   - both phase-3 re-reads use `include_deleted`: recording the outcome of a
     file that already left must not depend on the trash flag. Symmetrically,
     `soft_delete_invoice` refuses an UNSETTLED dispatch (same predicate as
     the credit-note guard) so the retry/resume path cannot be stranded by a
     trash landing in the unlocked dispatch window.
   - the **retry leg is a short prepare**: it re-checks role, environment,
     channel and mandate, then re-sends the FROZEN `inv.xml` verbatim. It
     deliberately skips the live re-validation and the totals recompute — the
     frozen document is the fiscal source of truth for the resend, and a
     changed live issuer/client card must be unable to block or diverge from
     it. It also stamps `sdi_resent_at`, the persisted proof of a resend.
5. **Retryability predicate** (service gate, SPA affordance, and the
   `create_credit_note` parent guard all share it): `state=transmitted AND
   identificativo_sdi IS NULL AND sdi_dispatch_started_at IS NOT NULL`,
   retry allowed once the lease expired (`409 invoice.transmit_in_progress`
   before). The lease being NULL distinguishes a **settled manual-export
   emission** (also ident-less) — never retryable. A retry re-sends the
   frozen `inv.xml` byte-identical under the same NomeFile, so if the lost
   attempt landed, the resend collides with SdI's own file-name dedupe
   instead of double-filing. A retry under a flipped SdI environment
   (`sdi_env_used` ≠ the runtime switch) is refused
   (`409 invoice.transmit_env_changed`): the dedupe net is per-environment.
6. **Lost-ACK reconcile (inbound)**: org resolution falls back from
   `IdentificativoSdI` to `NomeFile` (`sdi_resolve_invoice_org_by_filename`,
   SECURITY DEFINER, drafts excluded, migration 0079 + partial index). In
   `ingest_active_notification` the invoice lookup does the same, now under
   `FOR UPDATE`+`populate_existing` (the ingest races the unlocked dispatch
   window). Adoption of the notification's identifier is gated on the
   in-flight shape (`state=transmitted`); any other state archives the
   notification without transition. A notification that settles the invoice
   also clears the dispatch lease (the invoice stops advertising itself as
   retry-pending). An NS whose error codes are **all `00002` (nome file
   duplicato)** is the dedupe echo of our own resend and is archived without
   transition — but on the ident-MATCH branch only when `sdi_resent_at`
   proves a resend really happened (a pure-00002 NS on a first send whose
   name was burned by a pre-0079 rollback is a GENUINE scarto and rejects
   normally, or the 5-day correction window would be silently missed).
   `00404` (fattura duplicata) is deliberately NOT echo-swallowed — content
   checks never run on a name-deduped resend, so a 00404 always refers to a
   genuine competing filing and rejects normally.
7. **Public API idempotency resume**: the claim row is committed by the
   phase-1 checkpoint (it can no longer roll back with a failed request), so
   a snapshot-less claim would poison its key. Instead the transmit-carrying
   endpoints bind the claim to its invoice BEFORE dispatch
   (`idem.attach_invoice`); a same-key retry of an unsettled dispatch
   RESUMES that invoice (no duplicate compose, no orphan draft, no second
   numero) with the invoice-level lease arbitrating liveness — and when the
   invoice SETTLED in the meantime (the inbound reconcile adopted the ident,
   the main-line lost-ACK outcome), the resume returns its CURRENT state and
   stores the snapshot, converging the key to a 200 replay instead of
   409ing forever on a successfully filed document. Pure compose (no
   transmit) never checkpoints and keeps the original atomic semantics.
8. **Guards**: `create_credit_note` refuses a parent with an unsettled
   dispatch; `update_draft` refuses a `series` change once a number is
   allocated (a reverted/reopened draft would re-use its number under
   another sezionale); `reopen_rejected` also clears the lease.

## Consequences

- `transmitted -> draft` is now a legal, observable transition (a
  definitely-failed first dispatch). Integrators reading `/api/v1/events`
  must tolerate it; `sdi_dispatch_started_at` is exposed on both the SPA and
  public invoice payloads so an unsettled dispatch is distinguishable from a
  settled ident-less manual export. The SPA offers "Ritenta trasmissione" on
  exactly that predicate.
- A transmit-class request that reaches the pre-dispatch checkpoint consumes
  rate-limit budget even if the dispatch then fails (a real SdI attempt was
  made); the rate-limit module documents this exception.
- A one-shot compose+transmit whose dispatch fails leaves a durable numbered
  invoice (parked or reverted-to-draft) instead of nothing; the same-key
  retry resumes it, so no orphan is created by the idempotent path.
- `reopen_rejected` still deletes the invoice's notification rows, including
  archived duplicate-echo rows; the activity log keeps the audit entries.
  Late notifications for a reopened invoice's spent filename hit the state
  guard and are archived without transition.
- An invoice parked ambiguous with SdI permanently unreachable has no
  operator "abandon" action (accepted residual: retry or the inbound
  reconcile eventually settles it; an audit-logged abandon action can be a
  follow-up if it ever bites).
- `tenant_checkpoint` captures only the `app.*` GUC set: checkpointing
  inside a `with_actor` or `kg_allow_erase` window would drop the override
  (documented on the helper; the transmit path uses neither).

## Alternatives rejected

- **Outbox worker** (enqueue, background dispatcher with retries): equally
  durable, but it flips the synchronous transmit contract shipped on the
  SPA, `/api/v1` and MCP (all return the SdI sync result), and no generic
  outbound-delivery machinery exists to reuse (`event_outbox` is the
  ADR-0036 event bus, not a work queue). More moving parts for the same
  invariant.
- **Advisory locks as the in-flight arbiter**: session-scoped locks do not
  survive the pooled connection release at commit; a column lease does.
- **Echo-swallowing `00404`**: unsound — a genuine duplicate-numero scarto
  would be silently ignored and the 5-day correction window missed.
