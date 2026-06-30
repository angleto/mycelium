# ADR-0010 Conservation: free AdE service

Status: accepted. User's choice among the options presented.

## Context

Compliant substitutive conservation is a legal obligation (art. 39 DPR
633/72, 10 years; AgID Guidelines) on the data the system stores, and
was entirely absent in a draft. Options: free AdE service; third-party
qualified conservator via API; self-managed with a Conservation Officer
and Manual.

## Decision

Strategy = **free AdE service**. `ConservationProvider =
AdeFreeConservation`: Mycelium does not conserve in-house, it **tracks and
guides per-tenant adhesion** in the "Fatture e Corrispettivi" tax
portal (adhesion status on the Org profile) and relies on AdE
conservation of invoices that transited SdI.

## Consequences

- Minimal cost, no Conservation Officer/Manual burden on Mycelium.
- Adhesion is per VAT subject: Mycelium cannot adhere on the tenant's
  behalf, only guide/verify it.
- AdE conserves only what passes through SdI: invoices issued via
  `ManualExportChannel` in F7a are out of coverage and must be marked
  "tenant's responsibility". Effective coverage from F7b.
- The `ConservationProvider` abstraction is kept pluggable so a
  third-party conservator can be adopted later.

## Alternatives rejected

- Third-party qualified conservator: better risk transfer for a SaaS
  but recurring cost and integration, not chosen now.
- Self-managed: requires a Conservation Officer and Manual, packages,
  hashes, exhibition; high ongoing burden.
