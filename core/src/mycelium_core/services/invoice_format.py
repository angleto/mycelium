"""Pure formatting / arithmetic helpers for Italian e-invoicing,
extracted from ``services/invoice.py`` (#54). No DB access, no
session: every function takes already-loaded ORM objects (or plain
Decimals) and returns a value. Splitting these out keeps invoice.py
focused on the stateful lifecycle (draft -> transmit -> credit note)
while the deterministic FatturaPA 1.2 XML build + tax math live here.

Names are kept identical to their previous in-module form (incl. the
leading underscore on the private helpers) so invoice.py imports them
verbatim and every call site is unchanged — a pure code move, no
behavioural delta. The XML byte output is asserted by
test_f7_invoicing + test_invoices_forfettario.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from mycelium_core.models.client_profile import ClientProfile
from mycelium_core.models.invoice import (
    Invoice,
    InvoiceLine,
    InvoiceLineAltriDati,
    IssuerProfile,
)
from mycelium_core.sdi_channel import IntermediaryIdentity
from mycelium_core.services.payment_methods import resolve_payment

_NS = "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2"


def _q2(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _money(d: Decimal) -> str:
    return f"{_q2(d):.2f}"


def _q4(d: Decimal) -> Decimal:
    """The precision the line operands are STORED at: invoice_lines
    quantity is Numeric(12,4) and unit_price Numeric(14,4)."""
    return d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _amount4(d: Decimal) -> str:
    """Quantita / PrezzoUnitario, emitted at the 4 decimals we store.

    Emitting them at 2 while computing PrezzoTotale from the full stored
    value breaks the arithmetic SdI re-checks: unit_price 62.5432 x qty 2
    printed "62.54" against a PrezzoTotale of "125.09", but 2 x 62.54 is
    125.08 -> scarto. The XSD is far wider than 2 decimals (QuantitaType
    ``[0-9]{1,12}\\.[0-9]{2,8}``, PrezzoUnitario is Amount8DecimalType
    ``[\\-]?[0-9]{1,11}\\.[0-9]{2,8}``), so emitting 4 makes the operands
    the receiver re-multiplies exactly the ones we multiplied."""
    return f"{_q4(d):.4f}"


def _amount8(d: Decimal) -> str:
    """Amount8DecimalType (``[\\-]?[0-9]{1,11}\\.[0-9]{2,8}``): 2 to 8
    decimals, so a whole number still needs two ("3" is invalid, "3.00"
    is not). Trailing zeros beyond the second are trimmed because the
    backing column is Numeric(21,8) and would otherwise always print
    "3.00000000" on the wire."""
    s = f"{d.quantize(Decimal('1.00000000'), rounding=ROUND_HALF_UP):f}"
    intpart, _, dec = s.partition(".")
    return f"{intpart}.{dec.rstrip('0').ljust(2, '0')}"


def _line_total(quantity: Decimal, unit_price: Decimal) -> Decimal:
    """PrezzoTotale: the product of the operands AS EMITTED (4 decimals),
    rounded to the 2-decimal money the tracciato mandates. Going through
    _q4 first is what keeps ``PrezzoTotale == round(Quantita x
    PrezzoUnitario, 2)`` true for the receiver; for a DB-loaded line the
    quantize is a no-op (the columns already hold 4 decimals)."""
    return _q2(_q4(quantity) * _q4(unit_price))


@dataclass(frozen=True)
class Totals:
    taxable: Decimal
    vat: Decimal
    stamp_duty: Decimal
    total: Decimal


# --- forfettario (regime RF19) ---

# Virtual stamp duty (DM 17/06/2014): EUR 2.00 once the document's
# stamp_duty-relevant amount exceeds EUR 77.47. For a forfettario invoice the
# whole taxable is stamp_duty-relevant (no VAT line).
_BOLLO_THRESHOLD = Decimal("77.47")
_BOLLO_AMOUNT = Decimal("2.00")
_FORFETTARIO_NATURA = "N2.2"
# L. 190/2014 art. 1 commi 54-89: the mandatory purpose that identifies
# the forfettario regime on the invoice (verbatim, no trailing period).
FORFETTARIO_CAUSALE = (
    "Operazione effettuata in regime forfettario ai sensi dell'articolo 1, "
    "commi da 54 a 89, della Legge n. 190/2014 e successive modificazioni"
)
# Free-text dicitura printed on the human-readable PDF when the virtual
# stamp duty applies (it is not transmitted in the XML, only the
# structured DatiBollo is). Wording follows DM 17/06/2014 art.6 c.3 (the
# formula AdE itself uses on the courtesy PDF of a forfettario invoice).
BOLLO_DICITURA = "Bollo assolto ai sensi del decreto MEF 17 GIUGNO 2014 (ART. 6)"
# DatiRiepilogo/RiferimentoNormativo default for the forfettario regime when
# the issuer profile sets none (max 100 latin chars, XSD String100LatinType).
FORFETTARIO_RIFERIMENTO_NORMATIVO = (
    "Operazione in franchigia da IVA ai sensi dell'art.1, commi 54-89, L.190/2014"
)


def is_forfettario_causale(value: str | None) -> bool:
    """Whether a stored causale IS the statutory L.190/2014 dicitura.

    One definition for the two readers that must agree: the XML serializer,
    which appends the dicitura when it is not already there, and the courtesy
    PDF, which hangs the foreign-locale gloss off it. Compared after the same
    normalisation the XML applies, plus a strip, because a dicitura pasted into
    the Causale field with a trailing space is the same dicitura and the two
    call sites disagreeing about that is exactly how a document ends up
    carrying it twice."""
    if not value:
        return False
    return fatturapa_text(value).strip() == FORFETTARIO_CAUSALE


def _is_forfettario(issuer: IssuerProfile | None) -> bool:
    """Forfettario is regime RF19. Drives the line/purpose/stamp_duty
    defaults; every effect is overridable by an explicit caller value."""
    return issuer is not None and issuer.tax_regime == "RF19"


def _bollo_for(issuer: IssuerProfile | None, taxable: Decimal) -> Decimal:
    """EUR 2.00 virtual stamp duty on a forfettario invoice whose
    taxable reaches the legal threshold, else 0."""
    if _is_forfettario(issuer) and taxable >= _BOLLO_THRESHOLD:
        return _BOLLO_AMOUNT
    return Decimal(0)


def _resolve_line_tax(
    issuer: IssuerProfile | None,
    vat_rate: Decimal | None,
    vat_nature: str | None,
) -> tuple[Decimal, str | None]:
    """Resolve a line's (vat_rate, vat_nature). ``vat_rate=None`` means the
    caller did not specify one: forfettario -> 0% + Natura N2.2,
    ordinary regime -> the 22% default. An explicit vat_rate/vat_nature is
    always honoured (auto is only the default when unset)."""
    if vat_rate is None:
        if _is_forfettario(issuer):
            return _q2(Decimal(0)), vat_nature if vat_nature is not None else _FORFETTARIO_NATURA
        return _q2(Decimal(22)), vat_nature
    return _q2(vat_rate), vat_nature


def _riepilogo_groups(lines: Sequence[InvoiceLine]) -> dict[tuple[Decimal, str | None], Decimal]:
    """Group line totals by (vat_rate, vat_nature). Forfettario lines carry
    a Natura (N2.2) that the riepilogo must echo, so the key is the
    pair, not the rate alone (a 0% line with no Natura must not merge
    with a 0% N2.2 line)."""
    groups: dict[tuple[Decimal, str | None], Decimal] = {}
    for ln in lines:
        key = (ln.vat_rate, ln.vat_nature)
        # Same _line_total as the XML: the riepilogo imponibile must be
        # the sum of the PrezzoTotale values actually emitted.
        groups[key] = groups.get(key, Decimal(0)) + _line_total(ln.quantity, ln.unit_price)
    return groups


def _compute_totals(lines: Sequence[InvoiceLine], issuer: IssuerProfile | None = None) -> Totals:
    taxable = Decimal(0)
    vat = Decimal(0)
    for (rate, _natura), imponibile in _riepilogo_groups(lines).items():
        imp = _q2(imponibile)
        imposta = _q2(imp * rate / Decimal(100))
        taxable += imp
        vat += imposta
    taxable = _q2(taxable)
    vat = _q2(vat)
    stamp_duty = _bollo_for(issuer, taxable)
    return Totals(
        taxable=taxable, vat=vat, stamp_duty=stamp_duty, total=_q2(taxable + vat + stamp_duty)
    )


def _effective_iban(
    inv: Invoice, client: ClientProfile | None, issuer: IssuerProfile | None
) -> tuple[str | None, str | None]:
    """Resolve the payment IBAN AND its provenance.

    Precedence is invoice > client > issuer. The subtlety: create_draft
    copies the resolved IBAN into ``inv.payment_iban`` so it is
    visible/editable, which would erase the origin. To keep ``source``
    meaningful for the UI we classify ``inv.payment_iban`` as
    ``"invoice"`` (a genuine user override) only when it does NOT match
    the value the client/issuer would auto-supply; when it equals the
    upstream auto-fill we report that upstream origin instead. Returns
    (iban, source) with source "invoice"|"client"|"issuer"|None."""
    client_iban = client.payment_iban if client is not None else None
    issuer_iban = issuer.default_iban if issuer is not None else None
    if inv.payment_iban:
        if client_iban and inv.payment_iban == client_iban:
            return inv.payment_iban, "client"
        if issuer_iban and inv.payment_iban == issuer_iban:
            return inv.payment_iban, "issuer"
        return inv.payment_iban, "invoice"
    if client_iban:
        return client_iban, "client"
    if issuer_iban:
        return issuer_iban, "issuer"
    return None, None


# --- FatturaPA text charset (String*LatinType) ---


def is_basic_latin(value: str) -> bool:
    """XSD ``\\p{IsBasicLatin}`` is U+0000..U+007F. The C0 controls and DEL sit
    inside that block but are not emittable as XML text (and xs:normalizedString
    would rewrite tab/CR/LF anyway), so the printable range is what we accept."""
    return all("\x20" <= ch <= "\x7e" for ch in value)


def is_latin1(value: str) -> bool:
    """XSD ``[\\p{IsBasicLatin}\\p{IsLatin-1Supplement}]`` is U+0000..U+00FF.
    Same reasoning as above, plus the C1 controls (U+0080..U+009F) are excluded:
    what remains is printable ASCII + the accented Latin-1 range an Italian text
    actually needs."""
    return all("\x20" <= ch <= "\x7e" or "\xa0" <= ch <= "\xff" for ch in value)


#: Characters outside the Latin-1 facet that a provider or a person routinely
#: produces, each with the substitution carrying the same meaning. Explicit
#: rather than derived, because no decomposition rule turns U+20AC into "EUR"
#: and guessing is what this table exists to avoid. The euro sign is the one
#: that matters in practice: Stripe writes it into the line description of
#: every EUR subscription ("1 x Starter (at EUR50.00 / month)"), which is
#: exactly how a document that is otherwise perfect fails the schema on one
#: glyph. The incumbent (A-Cube) emits "EUR" for the same input.
#: Every key here is above U+00FF by construction; a character the facet
#: already admits never reaches this table (see ``fatturapa_text``).
_LATIN1_SUBSTITUTIONS = {
    # xs:normalizedString carries whiteSpace="replace", so SdI's own parser
    # turns each of these into a space BEFORE the pattern facet is applied: a
    # two-line note reaches the recipient as "riga uno riga due" today, and
    # validates. Mapping them here reproduces what the schema already does.
    # Dropping them instead (which is what the fallback below would do, since
    # they are outside the printable range) would weld the words together.
    "\n": " ",
    "\r": " ",
    "\t": " ",
    # Written as codepoints, not as glyphs: the entries ARE ambiguous
    # characters (that is why they are here), and a reader comparing this
    # table against a hex dump of a rejected document needs the number.
    "\u20ac": "EUR",  # EURO SIGN
    "\u2018": "'",  # LEFT SINGLE QUOTATION MARK
    "\u2019": "'",  # RIGHT SINGLE QUOTATION MARK (the typographic apostrophe)
    "\u201a": "'",  # SINGLE LOW-9 QUOTATION MARK
    "\u201c": '"',  # LEFT DOUBLE QUOTATION MARK
    "\u201d": '"',  # RIGHT DOUBLE QUOTATION MARK
    "\u201e": '"',  # DOUBLE LOW-9 QUOTATION MARK
    "\u2013": "-",  # EN DASH
    "\u2014": "-",  # EM DASH
    "\u2212": "-",  # MINUS SIGN
    "\u2022": "-",  # BULLET
    "\u2026": "...",  # HORIZONTAL ELLIPSIS
    "\u2007": " ",  # FIGURE SPACE
    "\u2009": " ",  # THIN SPACE
    "\u202f": " ",  # NARROW NO-BREAK SPACE
    "\u200b": "",  # ZERO WIDTH SPACE
    "\u200e": "",  # LEFT-TO-RIGHT MARK
    "\u200f": "",  # RIGHT-TO-LEFT MARK
    "\ufeff": "",  # ZERO WIDTH NO-BREAK SPACE / BOM
}


def fatturapa_text(value: str) -> str:
    """Reduce a text to what a FatturaPA ``String*LatinType`` facet admits.

    IDENTITY on text that already conforms: a character in U+0020..U+007E or
    U+00A0..U+00FF is returned untouched, so this sits on the emission path of
    documents that already validate without moving a byte of them. Only a
    character the facet rejects is rewritten -- by the table above, else by a
    compatibility decomposition keeping the Latin-1 parts (``ā`` -> ``a``,
    ``ﬁ`` -> ``fi``, ``½`` is already Latin-1 and is untouched). What survives
    neither is dropped: emitting it verbatim fails XSD validation and refuses a
    whole fiscal document over one glyph, which is the worse outcome of the two.

    Chosen over refusing the text upstream because the input is free text
    authored in another system (a Stripe product name, a counterpart's legal
    name) that the operator often cannot edit in time to invoice, and because
    the incumbent provider normalises the same input rather than bouncing it.
    Rejected alternative: transliterating with ``unicodedata.normalize`` over
    the WHOLE string, which rewrites ``é`` to ``e`` and would silently alter
    text the facet accepts as-is.

    What it does NOT do: enforce the facet's LENGTH. A substitution can grow a
    string (``€`` -> ``EUR``), so a caller slicing to a facet maximum must slice
    AFTER normalising -- see the ``Causale`` chunking in ``_build_xml``. A text
    that still overflows is caught by ``validate_fatturapa`` at the gate, which
    names the offending element. It also does not touch codes or identifiers:
    those are pure ASCII already, so normalisation is a no-op on them, and a
    malformed one (a six-digit CAP) is a data error no substitution can repair.
    """
    if is_latin1(value):
        return value
    out: list[str] = []
    for ch in value:
        if "\x20" <= ch <= "\x7e" or "\xa0" <= ch <= "\xff":
            out.append(ch)
        elif ch in _LATIN1_SUBSTITUTIONS:
            out.append(_LATIN1_SUBSTITUTIONS[ch])
        else:
            # Compatibility decomposition of THIS character only, keeping the
            # parts the facet admits. Combining marks and anything still
            # outside the range drop.
            out.append(
                "".join(c for c in unicodedata.normalize("NFKD", ch) if "\x20" <= c <= "\x7e")
            )
    return "".join(out)


def _sub(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
    """Every text node of the document passes through here, which is why the
    charset normalisation lives at this single point rather than at each of the
    forty-odd call sites: a field added later is covered by construction, and
    the normaliser is the identity on conformant text so no existing element
    changes."""
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = fatturapa_text(text)
    return el


def _bare_id_codice(vat_number: str, country_code: str | None) -> str:
    """Never assemble the country code into IdCodice. FatturaPA keeps the
    country (IdPaese) and the VAT number (IdCodice) in separate elements, so a
    value carrying a redundant leading country prefix matching IdPaese (e.g.
    ``IT01112223334`` with IdPaese ``IT``) must be emitted bare
    (``01112223334``) -- SdI rejects a country-prefixed IdCodice as malformed.
    The digit guard leaves a codice fiscale (3rd char is a letter) untouched,
    so we only strip a true country+VAT concatenation. This is the single
    backend chokepoint; callers/interfaces pass the stored value as-is."""
    code = (vat_number or "").strip()
    country_code = (country_code or "").upper()
    if (
        len(country_code) == 2
        and len(code) > 2
        and code[:2].upper() == country_code
        and code[2:3].isdigit()
    ):
        return code[2:]
    return code


def _fatturapa_phone(value: str | None) -> str | None:
    """Normalise a stored phone/fax to the FatturaPA ``Telefono``/``Fax``
    constraint (XSD pattern ``\\p{IsBasicLatin}{5,12}`` -- 5 to 12 BasicLatin
    chars). A user enters a human-formatted number (``+39 333 1234567``,
    spaces, separators, a ``+`` country prefix) that easily exceeds 12 chars;
    emitted verbatim it would *scarto the WHOLE document* on an OPTIONAL
    contact element. We reduce to bare digits (only the ``+`` and separators
    drop; the country-code digits stay, so an Italian number with prefix is 12
    digits and fits). If the digit string still falls outside 5-12 we return
    None so the caller OMITS the element: an optional courtesy contact must
    never invalidate a mandatory fiscal document. The stored value is left
    untouched (the PDF / contact display keeps the human format); this
    normalisation is only the FatturaPA emission boundary."""
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return digits if 5 <= len(digits) <= 12 else None


def _fatturapa_email(value: str | None) -> str | None:
    """Normalise a stored email to the FatturaPA ``EmailContattiType`` facet:
    length 7-256, pure BasicLatin (ASCII), AND the XSD pattern ``.+@.+[.]+.+``
    (something, ``@``, a host, a dot, a TLD). Trim surrounding whitespace; emit
    only when ALL hold, else None to OMIT the optional element rather than
    scarto the document. A malformed courtesy email (a typo, ``user@host`` with
    no dot, an IDN/unicode or too-short stub) must never invalidate the whole
    fiscal invoice -- same contract as the phone path above."""
    if not value:
        return None
    v = value.strip()
    if not v.isascii() or not (7 <= len(v) <= 256):
        return None
    return v if re.fullmatch(r".+@.+[.]+.+", v) else None


def _fatturapa_civico(value: str | None) -> str | None:
    """Normalise a stored civic number to the FatturaPA ``NumeroCivico``
    constraint (XSD ``String1-8`` -- 1 to 8 chars). Optional element: emit only
    when set and within length after trim, else None (omit). Putting the civic
    number inline in ``Indirizzo`` stays valid; this dedicated element is purely
    additive when the user fills the separate field."""
    if not value:
        return None
    v = value.strip()
    return v if 1 <= len(v) <= 8 else None


def _emit_anagrafica(
    parent: ET.Element, *, legal_name: str | None, first_name: str | None, last_name: str | None
) -> None:
    """FatturaPA Anagrafica is a choice: Denominazione OR Nome+Cognome. Emit
    Nome+Cognome for a persona fisica when BOTH are set, else Denominazione.
    Callers guarantee one mode is complete (_valid_anagrafica); the ``or ""``
    only avoids a None text node if that invariant is ever violated."""
    an = _sub(parent, "Anagrafica")
    if first_name and last_name:
        _sub(an, "Nome", first_name)
        _sub(an, "Cognome", last_name)
    else:
        _sub(an, "Denominazione", legal_name or "")


def _emit_altri_dati(dl: ET.Element, blocks: Sequence[InvoiceLineAltriDati]) -> None:
    """AltriDatiGestionali (2.2.1.16), 0..N per DettaglioLinee.

    XSD sequence: TipoDato (required, String10Type), then the optional
    RiferimentoTesto / RiferimentoNumero / RiferimentoData, in that
    order. An empty optional sub-element is OMITTED, never emitted
    blank: String60LatinType has minLength 1 and Amount8DecimalType has
    no empty form, so a blank tag scarta the document. ``ord`` (unique
    per line, migration 0088) is the emission order the user chose; we
    sort defensively here because a FatturaPA sequence is positional and
    the caller's row order is not guaranteed."""
    for b in sorted(blocks, key=lambda b: b.ord):
        adg = _sub(dl, "AltriDatiGestionali")
        _sub(adg, "TipoDato", b.tipo_dato)
        if b.riferimento_testo:
            _sub(adg, "RiferimentoTesto", b.riferimento_testo)
        if b.riferimento_numero is not None:
            _sub(adg, "RiferimentoNumero", _amount8(b.riferimento_numero))
        if b.riferimento_data is not None:
            _sub(adg, "RiferimentoData", b.riferimento_data.isoformat())


def _build_xml(
    inv: Invoice,
    fiscal: IssuerProfile,
    client: ClientProfile,
    lines: Sequence[InvoiceLine],
    progressivo: str,
    numero_override: str | None = None,
    collegata: tuple[str, dt.date] | None = None,
    intermediary: IntermediaryIdentity | None = None,
    altri_dati: Mapping[uuid.UUID, Sequence[InvoiceLineAltriDati]] | None = None,
) -> str:
    """``altri_dati`` maps an InvoiceLine id to its AltriDatiGestionali
    rows (invoice_line_altri_dati). Absent/empty -> the line emits no
    block at all, which is the default: the feature is opt-in per line."""
    ET.register_namespace("p", _NS)
    # FatturaPA transmission format: a 6-char CodiceDestinatario is a PA codice
    # univoco ufficio (B2G -> FPA12); a 7-char one (or the 0000000 default) is
    # B2B/B2C (FPR12). The format drives both ``versione`` and
    # ``FormatoTrasmissione`` and is the only structural difference for SdI
    # acceptance (CIG/CUP/split-payment are PA-side rifiuto concerns, not SdI
    # scarto, and the interop test does not validate file content).
    codice_dest = client.sdi_code or "0000000"
    fmt = "FPA12" if len(codice_dest) == 6 else "FPR12"
    root = ET.Element(f"{{{_NS}}}FatturaElettronica", versione=fmt)
    header = _sub(root, "FatturaElettronicaHeader")
    dt_ = _sub(header, "DatiTrasmissione")
    idt = _sub(dt_, "IdTrasmittente")
    if intermediary is not None:
        # Mycelium transmits as intermediary: the trasmittente is the accredited
        # channel holder, not the cedente (ADR-0011).
        _sub(idt, "IdPaese", intermediary.country_code)
        _sub(idt, "IdCodice", _bare_id_codice(intermediary.vat_number, intermediary.country_code))
    else:
        _sub(idt, "IdPaese", fiscal.country_code)
        # IdTrasmittente/IdCodice is validated by SdI as a CODICE FISCALE
        # (against the Anagrafe Tributaria), NOT as a P.IVA. For a company the
        # CF equals the P.IVA so either works; for a physical-person channel
        # holder (professionista/ditta individuale) the 16-char CF differs from
        # the 11-digit P.IVA and only the CF is accepted -- a P.IVA here is
        # scartata 00300 ("IdTrasmittente non valido"). So prefer the codice
        # fiscale, falling back to the P.IVA only when no CF is set.
        _sub(
            idt,
            "IdCodice",
            _bare_id_codice(fiscal.tax_code or fiscal.vat_number or "", fiscal.country_code),
        )
    _sub(dt_, "ProgressivoInvio", progressivo)
    _sub(dt_, "FormatoTrasmissione", fmt)
    _sub(dt_, "CodiceDestinatario", codice_dest)
    if not client.sdi_code and client.pec:
        # CodiceDestinatario "0000000" + recipient PEC: the cessionario's PEC
        # is its electronic address, so it goes in PECDestinatario (SdI routes
        # delivery to it). NOT ContattiTrasmittente/Email, which is the
        # transmitter's contact and is ignored for routing.
        _sub(dt_, "PECDestinatario", client.pec)
    cedente = _sub(header, "CedentePrestatore")
    anag = _sub(cedente, "DatiAnagrafici")
    if fiscal.vat_number:
        iva = _sub(anag, "IdFiscaleIVA")
        _sub(iva, "IdPaese", fiscal.country_code)
        _sub(iva, "IdCodice", _bare_id_codice(fiscal.vat_number, fiscal.country_code))
    if fiscal.tax_code:
        _sub(anag, "CodiceFiscale", fiscal.tax_code)
    _emit_anagrafica(
        anag, legal_name=fiscal.legal_name, first_name=fiscal.first_name, last_name=fiscal.last_name
    )
    _sub(anag, "RegimeFiscale", fiscal.tax_regime)
    sede = _sub(cedente, "Sede")
    _sub(sede, "Indirizzo", fiscal.address)
    # NumeroCivico (XSD order: after Indirizzo, before CAP). Optional and
    # additive: emitted only when the dedicated field is set and conformant.
    civico = _fatturapa_civico(fiscal.civic_number)
    if civico:
        _sub(sede, "NumeroCivico", civico)
    _sub(sede, "CAP", fiscal.postal_code)
    _sub(sede, "Comune", fiscal.city)
    if fiscal.province:
        _sub(sede, "Provincia", fiscal.province)
    _sub(sede, "Nazione", fiscal.country)
    # Optional Contatti (XSD: Telefono, Fax, Email, in that order). Emitted
    # only when at least one channel is set; SdI ignores them, but they are
    # standard FatturaPA and show up in viewers / on a printed PDF. Each value
    # is normalised to its XSD facet first (Telefono/Fax 5-12, Email 7-256
    # BasicLatin): an optional courtesy contact must never scarto the whole
    # document, so a non-conformant one is dropped rather than emitted as-is.
    # Per-contact visibility: the issuer may hide a contact from the emitted
    # invoice. ``is not False`` shows by default -- a DB-loaded profile carries
    # a real bool, while a transient/in-memory IssuerProfile (tests) leaves the
    # server_default unapplied (None), which must still render as "show".
    phone = _fatturapa_phone(fiscal.phone) if fiscal.show_phone is not False else None
    fax = _fatturapa_phone(fiscal.fax)
    email = _fatturapa_email(fiscal.email) if fiscal.show_email is not False else None
    if phone or fax or email:
        contatti = _sub(cedente, "Contatti")
        if phone:
            _sub(contatti, "Telefono", phone)
        if fax:
            _sub(contatti, "Fax", fax)
        if email:
            _sub(contatti, "Email", email)
    cess = _sub(header, "CessionarioCommittente")
    canag = _sub(cess, "DatiAnagrafici")
    if client.vat_number:
        civa = _sub(canag, "IdFiscaleIVA")
        _sub(civa, "IdPaese", client.country_code or "IT")
        _sub(civa, "IdCodice", _bare_id_codice(client.vat_number, client.country_code or "IT"))
    if client.tax_code:
        _sub(canag, "CodiceFiscale", client.tax_code)
    _emit_anagrafica(
        canag,
        legal_name=client.legal_name,
        first_name=client.first_name,
        last_name=client.last_name,
    )
    csede = _sub(cess, "Sede")
    _sub(csede, "Indirizzo", client.address or "")
    ccivico = _fatturapa_civico(client.civic_number)
    if ccivico:
        _sub(csede, "NumeroCivico", ccivico)
    _sub(csede, "CAP", client.postal_code or "")
    _sub(csede, "Comune", client.city or "")
    if client.province:
        _sub(csede, "Provincia", client.province)
    _sub(csede, "Nazione", client.country or "IT")
    # No 1.5 TerzoIntermediarioOSoggettoEmittente / 1.6 SoggettoEmittente here,
    # deliberately. The AdE binds both to EMISSION by a subject other than the
    # cedente ("Nei casi di documenti emessi da un soggetto diverso dal
    # cedente/prestatore va valorizzato l'elemento seguente", Allegato A
    # 2.1.6), and it gives the transmitter a field of its own, 1.1.1
    # IdTrasmittente, filled above. Mycelium holds a mandate to TRANSMIT, not
    # an incarico all'emissione ex art. 21 c.1 DPR 633/72, so declaring TZ
    # would assert a role it has not been given. See ADR-0053.
    body = _sub(root, "FatturaElettronicaBody")
    dg = _sub(body, "DatiGenerali")
    dgd = _sub(dg, "DatiGeneraliDocumento")
    _sub(dgd, "TipoDocumento", inv.document_type.value)
    _sub(dgd, "Divisa", inv.currency)
    _sub(dgd, "Data", (inv.issued_at or dt.datetime.now(tz=dt.UTC)).date().isoformat())
    _sub(dgd, "Numero", numero_override or f"{inv.series}-{inv.number}")
    # Virtual stamp duty: DatiBollo goes AFTER Numero and BEFORE
    # ImportoTotaleDocumento (FatturaPA 1.2 element order). Only when it
    # applies (forfettario with taxable >= threshold); ordinary regime
    # never emits it.
    if inv.stamp_duty and inv.stamp_duty > 0:
        db = _sub(dgd, "DatiBollo")
        _sub(db, "BolloVirtuale", "SI")
        _sub(db, "ImportoBollo", _money(inv.stamp_duty))
    # taxable + vat + stamp_duty (the stamp_duty is part of the document total).
    _sub(dgd, "ImportoTotaleDocumento", _money(inv.total))
    # Causale is String200LatinType, repeatable. Normalise BEFORE slicing:
    # a substitution can lengthen the text (see ``fatturapa_text``), so
    # slicing first would hand the facet a 202-character chunk.
    causali: list[str] = []
    if inv.purpose:
        causali.append(fatturapa_text(inv.purpose)[:200])
    # Free notes ride along as additional Causale lines.
    if inv.notes:
        notes = fatturapa_text(inv.notes)
        causali.extend(notes[i : i + 200] for i in range(0, len(notes), 200))
    # L.190/2014 art. 1 commi 54-89 requires the dicitura on a forfettario
    # invoice. It is ADDITIVE here rather than a value occupying ``purpose``:
    # Causale is maxOccurs="unbounded", so a document can carry both the
    # operator's own causale and the statutory one, and the statutory one can
    # no longer be displaced by anything a person types in the Causale field
    # or an integration supplies. Emitted only when not already present (the
    # create-time default at ``invoice.create_draft`` still fills ``purpose``
    # for a blank forfettario draft, and every already-drafted invoice carries
    # it there), so this adds an element to documents that were missing it and
    # changes nothing about the ones that were not.
    if _is_forfettario(fiscal) and not any(is_forfettario_causale(c) for c in causali):
        causali.append(FORFETTARIO_CAUSALE)
    for causale in causali:
        _sub(dgd, "Causale", causale)
    dbs = _sub(body, "DatiBeniServizi")
    for ln in lines:
        dl = _sub(dbs, "DettaglioLinee")
        _sub(dl, "NumeroLinea", str(ln.line_no))
        _sub(dl, "Descrizione", ln.description)
        _sub(dl, "Quantita", _amount4(ln.quantity))
        _sub(dl, "PrezzoUnitario", _amount4(ln.unit_price))
        _sub(dl, "PrezzoTotale", _money(_line_total(ln.quantity, ln.unit_price)))
        # AliquotaIVA is RateType: EXACTLY 2 decimals ([0-9]{1,3}\.[0-9]{2}),
        # and vat_rate is stored Numeric(5,2) -- no operand is lost here.
        _sub(dl, "AliquotaIVA", f"{ln.vat_rate:.2f}")
        if ln.vat_nature:
            _sub(dl, "Natura", ln.vat_nature)
        # Last element of the DettaglioLinee sequence (after Natura and
        # RiferimentoAmministrazione, which we do not emit); a FatturaPA
        # sequence is order-validated.
        if altri_dati and (blocks := altri_dati.get(ln.id)):
            _emit_altri_dati(dl, blocks)
    # Group by (rate, vat_nature): a forfettario riepilogo MUST echo the
    # line Natura (e.g. N2.2) right after AliquotaIVA and before
    # ImponibileImporto, or SdI rejects the document. Deterministic
    # order: by rate, then vat_nature ("" sorts before any code).
    groups = _riepilogo_groups(lines)
    # RiferimentoNormativo: the issuer's text, else the forfettario default;
    # emitted only for groups carrying a Natura, after Imposta (XSD order).
    rif_normativo = fiscal.legal_reference or (
        FORFETTARIO_RIFERIMENTO_NORMATIVO if _is_forfettario(fiscal) else None
    )
    for rate, vat_nature in sorted(groups, key=lambda k: (k[0], k[1] or "")):
        imp = _q2(groups[(rate, vat_nature)])
        rie = _sub(dbs, "DatiRiepilogo")
        _sub(rie, "AliquotaIVA", f"{rate:.2f}")
        if vat_nature:
            _sub(rie, "Natura", vat_nature)
        _sub(rie, "ImponibileImporto", _money(imp))
        _sub(rie, "Imposta", _money(_q2(imp * rate / Decimal(100))))
        if vat_nature and rif_normativo:
            _sub(rie, "RiferimentoNormativo", rif_normativo)
    # DatiPagamento is emitted whenever ANY payment metadata is present
    # (IBAN, due date, terms days, or an explicit override of
    # CondizioniPagamento / ModalitaPagamento). When everything is
    # default and no IBAN/due date is set, we still skip the block (no
    # information to carry); the resolver provides system defaults
    # (TP02 / MP05) only when the block is actually emitted.
    if (
        inv.payment_iban
        or inv.payment_due_date
        or inv.payment_terms_days is not None
        or inv.payment_conditions_code
        or inv.payment_method_code
    ):
        resolved = resolve_payment(inv, client, fiscal)
        pay = _sub(body, "DatiPagamento")
        _sub(pay, "CondizioniPagamento", resolved.condizioni)
        det = _sub(pay, "DettaglioPagamento")
        # XSD DettaglioPagamento order: ModalitaPagamento,
        # DataRiferimentoTerminiPagamento, GiorniTerminiPagamento,
        # DataScadenzaPagamento, ImportoPagamento, [..., IBAN, ...].
        _sub(det, "ModalitaPagamento", resolved.modalita)
        if resolved.terms_days is not None:
            _sub(det, "GiorniTerminiPagamento", str(resolved.terms_days))
        if inv.payment_due_date is not None:
            _sub(det, "DataScadenzaPagamento", inv.payment_due_date.isoformat())
        _sub(det, "ImportoPagamento", _money(inv.total))
        if inv.payment_iban:
            _sub(det, "IBAN", inv.payment_iban)
    if collegata is not None:
        # TD04: link the corrected invoice by its FISCAL number + date
        # (e.g. "A12", 2026-03-01), NOT the internal UUID.
        # DatiFattureCollegate/IdDocumento is the human document number SdI
        # expects; emitting a UUID here makes a real TD04 malformed.
        numero, data = collegata
        fc = ET.SubElement(dg, "DatiFattureCollegate")
        _sub(fc, "IdDocumento", numero)
        _sub(fc, "Data", data.isoformat())
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


__all__ = [
    "BOLLO_DICITURA",
    "FORFETTARIO_CAUSALE",
    "FORFETTARIO_RIFERIMENTO_NORMATIVO",
    "Totals",
    "_amount4",
    "_amount8",
    "_bollo_for",
    "_build_xml",
    "_compute_totals",
    "_effective_iban",
    "_is_forfettario",
    "_line_total",
    "_money",
    "_q2",
    "_q4",
    "_resolve_line_tax",
    "_riepilogo_groups",
    "_sub",
    "fatturapa_text",
    "is_basic_latin",
    "is_forfettario_causale",
    "is_latin1",
]
