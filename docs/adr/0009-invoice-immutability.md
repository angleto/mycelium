# ADR-0009 Invoice immutability, soft-delete carve-out

Status: accepted. Corrects a conflict in the generic draft.

## Context

FR-1 provides soft-delete with restore on all entities. An issued
invoice (transmitted to SdI and not rejected) is a fiscal document
subject to a ten-year conservation and integrity obligation. Generic
soft-delete/restore would break immutability and progressive
numbering.

## Decision

The invoice has a state machine: `draft` -> `transmitted` -> SdI
terminal states. Only `draft` is deletable. After emission the record
is append-only and immutable; the only logical "removal" is a TD04
credit note linked via `parent_invoice_id`. Explicit carve-out in FR-1:
soft-delete does not apply to issued invoices and conserved documents.
The progressive number per (Org, series, year) is allocated
concurrency-safe only at the draft -> transmitted transition and never
reused; since ADR-0046 it is committed durably BEFORE the SdI dispatch
(together with `ProgressivoInvio`/`NomeFile` and the frozen XML), so no
identifier that may have reached SdI can ever be rolled back and
reassigned. A definitely-failed first dispatch may legally return the
invoice to `draft` keeping its allocated identity for verbatim reuse
(see FR-9, ADR-0046).

## Consequences

- Numbering integrity and progressivity preserved under concurrency.
- Corrections only via TD04, as is standard practice.

## Alternatives rejected

- Uniform soft-delete on invoices too: violates immutability and
  progressivity, non-compliant.
