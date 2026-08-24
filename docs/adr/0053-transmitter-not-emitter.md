# ADR-0053: Mycelium is the soggetto trasmittente, never the soggetto emittente

Status: Accepted (2026-08-24)
Revises: ADR-0011 (SdI intermediary/mandate model), which argued the
transmitter role correctly and then, in its implementation status, committed to
a payload that also declares the EMITTER role. The argument never distinguished
*emissione* from *trasmissione*; it silently upgraded "we transmit under
mandate" into "we issue under mandate".
Relates to: ADR-0009 (invoice immutability: already-frozen XML is never
rewritten, so this change is prospective only), ADR-0010 (conservation),
ADR-0051 (inbound payment connectors, which compose documents through the same
serializer), `core/tests/test_sdi_mandate.py` (the guard that discriminates),
`core/tests/test_f7_invoicing.py`, `core/tests/test_invoice_xsd.py`.

## Context

FatturaPA has three roles and gives each its own place in the file. The AdE
*Allegato A -- Specifiche tecniche* (v1.9.1, §DEFINIZIONI) defines them
separately:

> per **Intermediario**, qualsiasi soggetto terzo, incaricato dal
> cedente/prestatore o dal cessionario/committente di *emettere o trasmettere o
> ricevere* per proprio conto le fatture elettroniche veicolate dal SdI;

> per **Soggetto emittente**, il cedente/prestatore o l'Intermediario da questi
> per proprio conto *incaricato all'emissione* della fattura elettronica;

> per **Soggetto trasmittente**, il cedente/prestatore o l'Intermediario da
> questi per proprio conto *incaricato di trasmettere* la fattura elettronica al
> SdI;

Field 1.6 `SoggettoEmittente` is named after the second role, and §2.1.6 gates
it on emission, never on transmission:

> Nei casi di documenti **emessi** da un soggetto diverso dal cedente/prestatore
> va valorizzato l'elemento seguente.

The vendored schema says the same about the block that accompanies it
(`Schema_VFPA12_V1.2.3.xsd`, `TerzoIntermediarioSoggettoEmittenteType`):

> Blocco relativo ai dati del Terzo Intermediario che **emette** fattura
> elettronica per conto del Cedente/Prestatore

The transmitter already has a field of its own, 1.1.1 `IdTrasmittente`, and
Provv. 89757/2018 §2.1 says that is all transmission requires: the
identification of the soggetto trasmittente "è rispettata utilizzando una delle
modalità di colloquio con il SdI", i.e. by the channel, not by anything in the
payload.

Mycelium was emitting all three: `IdTrasmittente`, plus
`TerzoIntermediarioOSoggettoEmittente` and `SoggettoEmittente=TZ`. The mandate
it actually holds is a transmission mandate. `SdiMandate.scope` says so in the
schema (`server_default="transmit"`), and nothing in the emission path ever
read it, so the XML asserted a role the database denied.

## Decision

**Mycelium's identity appears in `IdTrasmittente` and in the file name. Nothing
of Mycelium's appears in the document body.** Blocks 1.5 and 1.6 are not
emitted, by anyone, ever, on this code path.

`_payload_intermediary` stays and keeps its job, which is NOT what its old
docstring said. It selects who goes in `IdTrasmittente`: returning `None` for
self-transmission is what makes the cedente its own trasmittente, and the `None`
branch emits the cedente's **codice fiscale** rather than a P.IVA. SdI validates
`IdTrasmittente` as a codice fiscale against the Anagrafe Tributaria, and for a
physical-person channel holder the two differ, so deleting the helper as "1.5/1.6
scaffolding" would have silently produced scarto 00300.

`IntermediaryIdentity.legal_name` and the `MYCELIUM_SDI_INTERMEDIARY_DENOMINAZIONE`
setting are deleted. Their only consumer was the `<Denominazione>` inside the
removed block, and the setting was in the fail-closed startup check, so a
production switched to `sdicoop` was being rejected at boot over a value nothing
read.

## Consequences

- What SdI does about the change: nothing. `TerzoIntermediarioOSoggettoEmittente`
  appears in no error code, and `SoggettoEmittente` appears only inside the
  duplicate-document control 00404/00409, whose first branch treats "absent" and
  "TZ" identically. That asymmetry is the argument, not a weakness of it: the
  element carried zero technical upside and a non-zero declarative downside, so
  it had to be right on the merits because nothing would ever bounce it back.
- The change is **prospective only**. `Invoice.xml` is written once
  (`invoice.transmit`) and never rewritten, and an unsettled retry deliberately
  re-sends the frozen bytes under the same NomeFile (ADR-0009 + SdI dedupe). An
  invoice already transmitted with the block keeps it, and a retry will re-file
  it. That is correct and must not be "fixed".
- Documents composed for a self-transmitting issuer are unchanged: cedente VAT
  equals channel id, `_payload_intermediary` already returned `None`, and no
  block was emitted. In practice that covers everything mycelium has filed so
  far.
- If a genuine *incarico all'emissione* ex art. 21 c.1 DPR 633/72 is ever
  granted by a tenant, this decision is revisited rather than worked around:
  `SdiMandate.scope` grows an `emit` value, the scope gates the block, and this
  ADR is superseded. The role is a fact about a signed document, so it belongs
  in a column, not in an inference from "the channel holder differs from the
  cedente" — which is what the code used to infer it from.

## Alternatives rejected

**Keep TZ and argue it covers transmission.** The word AdE uses to gate the
field is *emessi*, the transmitter has a dedicated field, and every worked AdE
example of filling 1.5+1.6 is a real per-conto emission (cooperativa agricola
per il socio; tour operator ex art. 74-ter c.8), never "a provider transmits my
file". Stated honestly: no primary text says "a transmitter must not declare
TZ". The prohibition is an inference from the definitions, the gating verb, the
existence of `IdTrasmittente`, and art. 21 c.2 lett. n) which anchors 1.6 in
emission. It is well grounded but it is an inference, and anyone citing this ADR
should cite it as one.

**Gate the block on `SdiMandate.scope` now, defaulting to `transmit`.** That
leaves code no entry point reaches, which is a defect while it still works. The
gate arrives with the first mandate that actually grants emission, not before.

**Leave it and note the discrepancy.** The document has fiscal value and is kept
for ten years. A declaration in it that a named third party issued someone
else's invoice is not a comment.

## Amendment (2026-08-24) — the trasmittente's own code moves to platform configuration

This ADR left `MYCELIUM_SDI_INTERMEDIARY_ID_CODICE` where it found it, in an
env var, and that turned out to be the wrong home for the one field it made
load-bearing. With 1.5/1.6 gone, `IdTrasmittente` is the ONLY place the
channel's identity appears, and FatturaPA is specific about what belongs there:

> IdCodice: numero di identificazione fiscale del trasmittente (per i soggetti
> stabiliti nel territorio dello Stato Italiano corrisponde al Codice Fiscale).
> In caso di IdPaese uguale a IT, il sistema ne verifica la presenza in Anagrafe
> Tributaria: se non esiste come codice fiscale, il file viene scartato con
> codice errore 00300.
> — Allegato A, Specifiche tecniche 1.9.1, §2.1.1

A deployment can hold the wrong value there for months without knowing, because
the self-transmission branch takes a different path (it emits the cedente's own
`tax_code`) and only the intermediary branch — transmitting for a DIFFERENT
tenant — exposes it. A shadow document produced by the dry-run feature is what
surfaced it: cedente HahnBanach, trasmittente the channel holder, `IdCodice`
carrying an 11-digit P.IVA belonging to a physical person, whose codice fiscale
is the 16-character form.

So the value now lives in `system_settings`, editable by an admin, with the env
var kept as a fallback while the column is blank (migration `0004`; the migrate
Job sees only the database URL, so it cannot seed from the environment and the
move has to be expand-only). Two consequences follow:

- the fail-closed boot check no longer demands it. It could not: refusing to
  start a deployment that is configured correctly in the database would be the
  guard working against the reason the value moved. Presence is enforced in
  `invoice.transmit` instead, upstream of the number, the NomeFile and the
  frozen XML, so a missing one costs nothing durable;
- `esito_committente` stops falling back to `"0"`. That fabricated
  `IT0_XXXXX_ES_001.xml` and POSTed it to SdI with the failure swallowed into a
  log warning, and it was only ever safe because the boot check made the value
  non-empty for the process lifetime.

The shape is validated on save; the one thing a shape cannot decide — 11 digits
being right for a company and wrong for a person — is shown as a warning beside
the field rather than refused, because refusing it would block every company.
