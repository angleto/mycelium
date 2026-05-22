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
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from flow_core.models.client_profile import ClientProfile
from flow_core.models.invoice import Invoice, InvoiceLine, IssuerProfile

_NS = "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2"


def _q2(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _money(d: Decimal) -> str:
    return f"{_q2(d):.2f}"


@dataclass(frozen=True)
class Totals:
    taxable: Decimal
    vat: Decimal
    bollo: Decimal
    total: Decimal


# --- forfettario (regime RF19) ---

# Virtual stamp duty (DM 17/06/2014): EUR 2.00 once the document's
# bollo-relevant amount exceeds EUR 77.47. For a forfettario invoice the
# whole taxable is bollo-relevant (no VAT line).
_BOLLO_THRESHOLD = Decimal("77.47")
_BOLLO_AMOUNT = Decimal("2.00")
_FORFETTARIO_NATURA = "N2.2"
# L. 190/2014 art. 1 commi 54-89: the mandatory causale that identifies
# the forfettario regime on the invoice (verbatim, no trailing period).
FORFETTARIO_CAUSALE = (
    "Operazione effettuata in regime forfettario ai sensi dell'articolo 1, "
    "commi da 54 a 89, della Legge n. 190/2014 e successive modificazioni"
)
# Free-text dicitura printed on the human-readable PDF when the virtual
# stamp duty applies (it is not transmitted in the XML, only the
# structured DatiBollo is).
BOLLO_DICITURA = "Imposta di bollo assolta in modo virtuale"


def _is_forfettario(issuer: IssuerProfile | None) -> bool:
    """Forfettario is regime RF19. Drives the line/causale/bollo
    defaults; every effect is overridable by an explicit caller value."""
    return issuer is not None and issuer.regime_fiscale == "RF19"


def _bollo_for(issuer: IssuerProfile | None, taxable: Decimal) -> Decimal:
    """EUR 2.00 virtual stamp duty on a forfettario invoice whose
    taxable reaches the legal threshold, else 0."""
    if _is_forfettario(issuer) and taxable >= _BOLLO_THRESHOLD:
        return _BOLLO_AMOUNT
    return Decimal(0)


def _resolve_line_tax(
    issuer: IssuerProfile | None,
    vat_rate: Decimal | None,
    natura: str | None,
) -> tuple[Decimal, str | None]:
    """Resolve a line's (vat_rate, natura). ``vat_rate=None`` means the
    caller did not specify one: forfettario -> 0% + Natura N2.2,
    ordinary regime -> the 22% default. An explicit vat_rate/natura is
    always honoured (auto is only the default when unset)."""
    if vat_rate is None:
        if _is_forfettario(issuer):
            return _q2(Decimal(0)), natura if natura is not None else _FORFETTARIO_NATURA
        return _q2(Decimal(22)), natura
    return _q2(vat_rate), natura


def _riepilogo_groups(lines: Sequence[InvoiceLine]) -> dict[tuple[Decimal, str | None], Decimal]:
    """Group line totals by (vat_rate, natura). Forfettario lines carry
    a Natura (N2.2) that the riepilogo must echo, so the key is the
    pair, not the rate alone (a 0% line with no Natura must not merge
    with a 0% N2.2 line)."""
    groups: dict[tuple[Decimal, str | None], Decimal] = {}
    for ln in lines:
        key = (ln.vat_rate, ln.natura)
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
    bollo = _bollo_for(issuer, taxable)
    return Totals(taxable=taxable, vat=vat, bollo=bollo, total=_q2(taxable + vat + bollo))


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


def _build_xml(
    inv: Invoice,
    fiscal: IssuerProfile,
    client: ClientProfile,
    lines: Sequence[InvoiceLine],
    progressivo: str,
    numero_override: str | None = None,
) -> str:
    ET.register_namespace("p", _NS)
    root = ET.Element(f"{{{_NS}}}FatturaElettronica", versione="FPR12")
    header = _sub(root, "FatturaElettronicaHeader")
    dt_ = _sub(header, "DatiTrasmissione")
    idt = _sub(dt_, "IdTrasmittente")
    _sub(idt, "IdPaese", fiscal.paese)
    _sub(idt, "IdCodice", fiscal.piva or fiscal.codice_fiscale or "")
    _sub(dt_, "ProgressivoInvio", progressivo)
    _sub(dt_, "FormatoTrasmissione", "FPR12")
    _sub(dt_, "CodiceDestinatario", client.codice_destinatario or "0000000")
    if not client.codice_destinatario and client.pec:
        cc = _sub(dt_, "ContattiTrasmittente")
        _sub(cc, "Email", client.pec)
    cedente = _sub(header, "CedentePrestatore")
    anag = _sub(cedente, "DatiAnagrafici")
    if fiscal.piva:
        iva = _sub(anag, "IdFiscaleIVA")
        _sub(iva, "IdPaese", fiscal.paese)
        _sub(iva, "IdCodice", fiscal.piva)
    if fiscal.codice_fiscale:
        _sub(anag, "CodiceFiscale", fiscal.codice_fiscale)
    an = _sub(anag, "Anagrafica")
    _sub(an, "Denominazione", fiscal.denominazione)
    _sub(anag, "RegimeFiscale", fiscal.regime_fiscale)
    sede = _sub(cedente, "Sede")
    _sub(sede, "Indirizzo", fiscal.indirizzo)
    _sub(sede, "CAP", fiscal.cap)
    _sub(sede, "Comune", fiscal.comune)
    if fiscal.provincia:
        _sub(sede, "Provincia", fiscal.provincia)
    _sub(sede, "Nazione", fiscal.nazione)
    cess = _sub(header, "CessionarioCommittente")
    canag = _sub(cess, "DatiAnagrafici")
    if client.id_codice:
        civa = _sub(canag, "IdFiscaleIVA")
        _sub(civa, "IdPaese", client.id_paese or "IT")
        _sub(civa, "IdCodice", client.id_codice)
    if client.codice_fiscale:
        _sub(canag, "CodiceFiscale", client.codice_fiscale)
    can = _sub(canag, "Anagrafica")
    _sub(can, "Denominazione", client.ragione_sociale)
    csede = _sub(cess, "Sede")
    _sub(csede, "Indirizzo", client.indirizzo or "")
    _sub(csede, "CAP", client.cap or "")
    _sub(csede, "Comune", client.comune or "")
    if client.provincia:
        _sub(csede, "Provincia", client.provincia)
    _sub(csede, "Nazione", client.nazione or "IT")

    body = _sub(root, "FatturaElettronicaBody")
    dg = _sub(body, "DatiGenerali")
    dgd = _sub(dg, "DatiGeneraliDocumento")
    _sub(dgd, "TipoDocumento", inv.document_type.value)
    _sub(dgd, "Divisa", inv.currency)
    _sub(dgd, "Data", (inv.issued_at or dt.datetime.now(tz=dt.UTC)).date().isoformat())
    _sub(dgd, "Numero", numero_override or f"{inv.series}{inv.number}")
    # Virtual stamp duty: DatiBollo goes AFTER Numero and BEFORE
    # ImportoTotaleDocumento (FatturaPA 1.2 element order). Only when it
    # applies (forfettario with taxable >= threshold); ordinary regime
    # never emits it.
    if inv.bollo and inv.bollo > 0:
        db = _sub(dgd, "DatiBollo")
        _sub(db, "BolloVirtuale", "SI")
        _sub(db, "ImportoBollo", _money(inv.bollo))
    # taxable + vat + bollo (the bollo is part of the document total).
    _sub(dgd, "ImportoTotaleDocumento", _money(inv.total))
    if inv.causale:
        _sub(dgd, "Causale", inv.causale)
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
        if ln.natura:
            _sub(dl, "Natura", ln.natura)
    # Group by (rate, natura): a forfettario riepilogo MUST echo the
    # line Natura (e.g. N2.2) right after AliquotaIVA and before
    # ImponibileImporto, or SdI rejects the document. Deterministic
    # order: by rate, then natura ("" sorts before any code).
    groups = _riepilogo_groups(lines)
    for rate, natura in sorted(groups, key=lambda k: (k[0], k[1] or "")):
        imp = _q2(groups[(rate, natura)])
        rie = _sub(dbs, "DatiRiepilogo")
        _sub(rie, "AliquotaIVA", f"{rate:.2f}")
        if natura:
            _sub(rie, "Natura", natura)
        _sub(rie, "ImponibileImporto", _money(imp))
        _sub(rie, "Imposta", _money(_q2(imp * rate / Decimal(100))))
    if inv.payment_iban or inv.payment_due_date:
        # MP05 = bonifico; TP02 = pagamento completo (single payment).
        pay = _sub(body, "DatiPagamento")
        _sub(pay, "CondizioniPagamento", "TP02")
        det = _sub(pay, "DettaglioPagamento")
        _sub(det, "ModalitaPagamento", "MP05")
        if inv.payment_due_date is not None:
            _sub(det, "DataScadenzaPagamento", inv.payment_due_date.isoformat())
        _sub(det, "ImportoPagamento", _money(inv.total))
        if inv.payment_iban:
            _sub(det, "IBAN", inv.payment_iban)
    if inv.parent_invoice_id is not None:
        # TD04: link the corrected invoice.
        fc = ET.SubElement(dg, "DatiFattureCollegate")
        _sub(fc, "IdDocumento", str(inv.parent_invoice_id))
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


__all__ = [
    "BOLLO_DICITURA",
    "FORFETTARIO_CAUSALE",
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
