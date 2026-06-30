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
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from mycelium_core.models.client_profile import ClientProfile
from mycelium_core.models.invoice import Invoice, InvoiceLine, IssuerProfile
from mycelium_core.sdi_channel import IntermediaryIdentity
from mycelium_core.services.payment_methods import resolve_payment

_NS = "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2"


def _q2(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _money(d: Decimal) -> str:
    return f"{_q2(d):.2f}"


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
        groups[key] = groups.get(key, Decimal(0)) + _q2(ln.quantity * ln.unit_price)
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


def _sub(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = text
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
    """Normalise a stored email to the FatturaPA ``Email`` constraint (XSD
    pattern ``\\p{IsBasicLatin}{7,256}`` -- 7 to 256 BasicLatin chars). Trim
    surrounding whitespace; emit only when the result is pure BasicLatin
    (ASCII) and within length, else None to OMIT the optional element rather
    than scarto the document (e.g. an IDN/unicode address, or a stub shorter
    than 7 chars)."""
    if not value:
        return None
    v = value.strip()
    if not v.isascii():
        return None
    return v if 7 <= len(v) <= 256 else None


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
    parent: ET.Element, *, legal_name: str, first_name: str | None, last_name: str | None
) -> None:
    """FatturaPA Anagrafica is a choice: Denominazione OR Nome+Cognome. Emit
    Nome+Cognome for a persona fisica when BOTH are set, else Denominazione."""
    an = _sub(parent, "Anagrafica")
    if first_name and last_name:
        _sub(an, "Nome", first_name)
        _sub(an, "Cognome", last_name)
    else:
        _sub(an, "Denominazione", legal_name)


def _build_xml(
    inv: Invoice,
    fiscal: IssuerProfile,
    client: ClientProfile,
    lines: Sequence[InvoiceLine],
    progressivo: str,
    numero_override: str | None = None,
    collegata: tuple[str, dt.date] | None = None,
    intermediary: IntermediaryIdentity | None = None,
) -> str:
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
    if intermediary is not None:
        # Mycelium as terzo intermediario / soggetto emittente. Header order:
        # after CessionarioCommittente, then SoggettoEmittente.
        terzo = _sub(header, "TerzoIntermediarioOSoggettoEmittente")
        tanag = _sub(terzo, "DatiAnagrafici")
        tiva = _sub(tanag, "IdFiscaleIVA")
        _sub(tiva, "IdPaese", intermediary.country_code)
        _sub(tiva, "IdCodice", _bare_id_codice(intermediary.vat_number, intermediary.country_code))
        tan = _sub(tanag, "Anagrafica")
        _sub(tan, "Denominazione", intermediary.legal_name)
        # TZ = document transmitted by a third party on the cedente's behalf.
        _sub(header, "SoggettoEmittente", "TZ")

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
    if inv.purpose:
        _sub(dgd, "Causale", inv.purpose)
    # Free notes ride along as additional Causale lines (FatturaPA
    # Causale is repeatable, max 200 chars each).
    if inv.notes:
        for i in range(0, len(inv.notes), 200):
            _sub(dgd, "Causale", inv.notes[i : i + 200])
    dbs = _sub(body, "DatiBeniServizi")
    for ln in lines:
        dl = _sub(dbs, "DettaglioLinee")
        _sub(dl, "NumeroLinea", str(ln.line_no))
        _sub(dl, "Descrizione", ln.description)
        _sub(dl, "Quantita", f"{ln.quantity:.2f}")
        _sub(dl, "PrezzoUnitario", f"{ln.unit_price:.2f}")
        line_total = _q2(ln.quantity * ln.unit_price)
        _sub(dl, "PrezzoTotale", _money(line_total))
        _sub(dl, "AliquotaIVA", f"{ln.vat_rate:.2f}")
        if ln.vat_nature:
            _sub(dl, "Natura", ln.vat_nature)
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
    "_bollo_for",
    "_build_xml",
    "_compute_totals",
    "_effective_iban",
    "_is_forfettario",
    "_money",
    "_q2",
    "_resolve_line_tax",
    "_riepilogo_groups",
    "_sub",
]
