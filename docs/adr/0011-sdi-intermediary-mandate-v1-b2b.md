# ADR-0011 SDI: intermediary/mandate model, v1 B2B/B2C

Status: accepted. Corrects a legal error in an earlier draft.

## Context

A draft decided "direct accreditation, no third-party intermediary" as
a distinguishing feature. In a multi-tenant SaaS this is legally wrong:
the moment a single accredited channel transmits invoices whose
`CedentePrestatore` belongs to different tenants, the operator **is** a
transmitter on behalf of third parties, i.e. an intermediary. The
channel's mutual-TLS certificate is tied to a single tax code (AdE CA)
and carries no per-tenant identity; per-tenant accreditation is
infeasible.

## Decision

A **single shared channel**; the tenant's identity in the **FatturaPA
payload** (`CedentePrestatore`, `TerzoIntermediarioOSoggettoEmittente`),
never in the TLS identity. An explicit per-issuer-profile (per VAT
subject) `SdiMandate` model (scope, validity, revocation, audit): Flow
transmits on the tenant's
behalf under mandate and assumes the intermediary's operational duties
(always-on inbound SOAP endpoint, notification correlation by
`IdentificativoSdI`, audit). v1 = **B2B/B2C only**: no signature (not
required via an accredited channel), active-cycle notifications RC, MC,
NS, AT. **Post-v1**: PA/B2G (CAdES/XAdES signature + qualified
certificate, NE/DT/EC/SE), passive cycle, reverse charge/self-billing,
foreign clients, quarterly stamp duty. Phased rollout (F7a manual, F7b
SdICoop test, F7c production).

## Consequences

- `IdentificativoSdI` is a first-class indexed column to correlate
  push notifications to the right tenant.
- F7c (service agreement + accreditation + always-on inbound mutual-TLS
  endpoint) is the heaviest item and must be resourced as such.
- The v1 fiscal scope is explicitly minimal; the rest is declared as
  deferred, not implicitly "complete".

## Alternatives rejected

- "Direct accreditation with no intermediary" in multi-tenant: legally
  wrong and technically infeasible (one channel = one tax code).
- Third-party intermediary via API: valid but rejected because the user
  wants no third party seeing the invoices; the mandate model with our
  own channel satisfies the constraint while staying legally correct.

## Implementation status (2026-05-22)

F7b implemented as code, config-gated on `FLOW_SDI_CHANNEL=sdicoop`: the
`SdiMandate` is keyed per issuer profile (per VAT subject), not per Org,
because one Org may hold several VAT subjects each authorizing transmission
independently (consistent with the per-P.IVA `conservation_adhesion`). The
intermediary payload (`IdTrasmittente` = channel holder,
`TerzoIntermediarioOSoggettoEmittente`, `SoggettoEmittente=TZ`), the SdICoop
`RiceviFile` SOAP client (mutual TLS, per-intermediary file name /
ProgressivoInvio), the inbound `/sdi/notification` receiver (cross-org
correlation by `IdentificativoSdI` via a SECURITY DEFINER resolver; FORCE
RLS dropped on `invoices` per the 0068 pattern) and official-XSD validation
are all present. The live SOAP transport is never exercised in CI; the exact
WSDL namespace/operation, the SOAP esito and whether WS-Security signing is
required are to be verified against the AdE test environment at
accreditation. Signature (CAdES/XAdES) + PA/B2G remain deferred.
