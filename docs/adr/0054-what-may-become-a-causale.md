# ADR-0054: What may become a Causale, and where the tracciato's charset is enforced

Status: Accepted (2026-08-24)
Revises: ADR-0051 (inbound payment connectors), which already argued that
`purpose` and `notes` become `Causale` and must not be contaminated by
labelling — and then let the Stripe mapper fill `purpose` from the provider's
customer-facing free text, which is the same contamination arriving by the
other door.
Relates to: ADR-0009 (invoice immutability), ADR-0053 (transmitter, not
emitter), `core/tests/test_invoice_causale_charset.py`,
`core/tests/test_payment_connector_mappers.py`,
`core/tests/test_payment_connectors_service.py`.

## Context

Three findings from one document. A live Stripe connector composed a FatturaPA
invoice whose `<Causale>` read:

> Per assicurarsi che la fattura elettronica venga inviata, per piacere controlli
> di aver inserito tutti i dati: vada su <site>, "Login", "Configura" e
> "Fatturazione".

That text is the Stripe invoice `description`, which the Stripe dashboard labels
**Memo**. It is written by the merchant *to the customer*, and Stripe renders it
on the hosted invoice and on the PDF, twice (the identical string is also the
`footer`). The mapper read it as if it were a fiscal causale. `Causale` (2.1.1.11)
is the description of the operation for SdI, for the customer's commercialista
and for an auditor years later; onboarding copy in it is a category error, and
the document is kept in ten-year conservazione.

The same document was also **schema-invalid**, in two independent ways, and
nothing had told anyone:

```
Element 'CAP': the value '202129' is not accepted by the pattern '[0-9]{5}'
Element 'Descrizione': the value '1 × Starter (at €50.00 / month)' is not
  accepted by the pattern '[\p{IsBasicLatin}\p{IsLatin-1Supplement}]{1,1000}'
```

The euro sign (U+20AC, Currency Symbols block) is outside every
`String*LatinType` facet. Stripe writes it into the line description of every
EUR subscription item by itself, so this is the default path for a whole class
of invoices, not an edge case. It never reached SdI: the XSD gate in
`transmit()` refuses first. But `get_xml_preview` did not run that gate, so the
preview handed back a downloadable document that could never be filed, and a
connector dry-run reported a clean shadow run on it.

Third: on a forfettario (RF19) issuer, the mandatory L.190/2014 dicitura was a
*create-time default on the single `purpose` slot*. Anything else in that slot —
a causale a person typed, a value an integration supplied — displaced the
statutory wording from both the XML and the PDF, silently.

## Decision

**1. A provider's free text never becomes a Causale.** On the connector path
`purpose` comes from `connector.default_purpose` and nothing else: the value
somebody chose knowing it would be the causale. The provider's own text keeps
describing the supply where a description belongs, in the line `<Descrizione>`.

**2. The statutory forfettario dicitura is additive, in the serializer.**
`Causale` is `maxOccurs="unbounded"`, so a document carries the operator's
causale *and* the dicitura. The create-time default stays, so every existing
draft still serialises to exactly one element; the serializer appends only when
the dicitura is not already present. One predicate, `is_forfettario_causale`,
answers "is this the dicitura" for both the XML and the PDF, comparing
normalised-and-stripped text — the two call sites disagreeing on that is how a
document ends up carrying it twice. The courtesy PDF mirrors the serializer, so
the legal and the courtesy document say the same thing.

**3. The Latin-1 facet is enforced by normalisation at one point: `_sub`.**
Every text node of every document passes through that helper, so a field added
later is covered by construction. `fatturapa_text` is the **identity** on text
the facet already admits, which is what makes it safe to put on the emission
path of documents that validate today. Outside that range it applies an explicit
substitution table (`€` → `EUR`, typographic quotes and dashes → ASCII, CR/LF/TAB
→ space), then a per-character compatibility decomposition keeping the Latin-1
parts, and drops what survives neither.

CR/LF/TAB deserve their own sentence, because deleting them was a regression we
wrote and caught: `xs:normalizedString` carries `whiteSpace="replace"`, so SdI's
own parser already turns them into spaces *before* applying the pattern facet. A
two-line note validates today and arrives as "riga uno riga due". Dropping the
character instead would have welded the words together.

**4. The XSD gate runs on the preview too.** Same call, same `DomainError`, same
element-level message. A preview that is byte-faithful but not validity-faithful
is worse than no preview: it is a promise.

**5. The issuer's CAP is validated where it is typed.** `CAPType` is `[0-9]{5}`
and 1.2.2.3 `<CAP>` is `<1.1>` in the tracciato. Refused at the write boundary
so the person editing the profile gets the error on that field, instead of every
future invoice sticking in draft with a schema message pointing at nothing.

## Consequences

- Transliteration widens what the system is willing to file. `Škoda Auto a.s.`
  used to be refused at the gate and is now emitted as `Skoda Auto a.s.`. This
  is deliberate and it is not free: a `Denominazione` is fiscal identity, not
  free text. It is accepted because the tracciato physically cannot carry `Š`,
  so the alternative is refusing to invoice a Czech company at all, and because
  the courtesy PDF still shows the real spelling (it is not bound by the
  tracciato). A name with no Latin-1 rendering at all still blocks, now with a
  length message rather than a pattern one.
- A counterpart's postal code is stored as the counterpart really writes it, and
  is NOT normalised. The AdE material consulted prescribes a conventional value
  for `CodiceDestinatario` towards non-established subjects (`XXXXXXX`) and
  prescribes nothing for `CAP`. Inventing a placeholder would be inventing
  fiscal data. A non-conformant one is therefore refused by the gate, which now
  fires at preview time rather than at transmit.
- The connector loses the ability to carry a per-document causale from the
  provider. `default_purpose` covers the real need. If a per-document one is
  ever wanted it arrives as an explicit, opt-in metadata key following the
  `metadata_<field>_keys` convention already used for VAT, codice fiscale and
  PEC — never by reading a field the provider defined for something else.
- Documents already transmitted are untouched: `Invoice.xml` is frozen
  (ADR-0009).

## Alternatives rejected

**Refuse non-conformant text instead of normalising it.** Every euro
subscription would park its event and stop invoicing until someone edited a
product name in Stripe. The incumbent provider normalises the same input rather
than bouncing it.

**Transliterate the whole string with `unicodedata.normalize`.** It rewrites `é`
to `e`, silently altering text the facet accepts as-is. The identity-on-valid
invariant is the property that makes this safe to deploy onto live invoicing;
whole-string decomposition destroys it.

**Normalise at the connector ingress instead.** The charset constraint is a
property of the output format, not of Stripe, and enforcing it at ingress leaves
every other writer (the SPA, the public compose API, the MCP tools) uncovered.
One rule, one implementation site, at the boundary that owns the rule.

**Keep the forfettario dicitura as a create-time default and simply refuse to
overwrite `purpose`.** That trades one silent failure for another: the operator
would type a causale and never see it. Both belong on the document, and the
tracciato already allows both.
