"""Human-readable A4 invoice PDF (the courtesy copy, not the legal
document: the legal document is the FatturaPA XML transited via SdI).

Pure-Python (reportlab built-in fonts only, no external font files).
The forfettario diciture (L.190/2014 purpose, and the virtual-stamp
note when stamp_duty applies) are printed verbatim in Italian and followed
by a parenthetical English-language description when the client's
locale is non-Italian: the legal value is in the original wording,
the parenthetical is descriptive only, mirroring how AdE itself
prints bilingual invoices for foreign cessionari.

The locale is the client's ``invoice_language`` (BCP47 tag); NULL
means ``it``. The FatturaPA XML is never translated (the legal
document is in Italian, see ``invoice_format._build_xml``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    HRFlowable,
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from mycelium_core.models.client_profile import ClientProfile
from mycelium_core.models.invoice import Invoice, InvoiceLine, IssuerProfile
from mycelium_core.services.date_format import format_date
from mycelium_core.services.invoice_format import (
    BOLLO_DICITURA,
    FORFETTARIO_CAUSALE,
    Totals,
    _is_forfettario,
)
from mycelium_core.services.payment_methods import (
    MODALITA_PAGAMENTO,
    resolve_payment,
)

# Supported PDF locales. NULL or any unknown tag falls back to ``it``.
# Keep the matrix small: this is courtesy text on a courtesy document,
# not the legal value. Add a language by extending both dicts together;
# the build raises KeyError if a label is missing for a locale (catches
# half-translated tables in CI).
_LABELS: dict[str, dict[str, str]] = {
    "it": {
        "invoice": "Fattura",
        "draft": "BOZZA",
        "issuer": "Cedente / Prestatore",
        "client": "Cessionario / Committente",
        "number": "Numero",
        "date": "Data",
        "doc_type": "Tipo documento",
        "tax_regime": "Regime fiscale",
        "sdi_code": "Codice Destinatario",
        "pec": "PEC",
        "description": "Descrizione",
        "quantity": "Quantità",
        "unit_price": "Prezzo unitario",
        "vat_rate": "Aliquota IVA %",
        "vat_nature": "Natura",
        "total": "Totale",
        "taxable": "Imponibile",
        "vat": "IVA",
        "stamp_duty": "Imposta di bollo",
        "doc_total": "Totale documento",
        "payment": "Pagamento",
        "method": "Modalità",
        "iban": "IBAN",
        "terms_days": "Giorni termini",
        "due_date": "Scadenza",
    },
    "en": {
        "invoice": "Invoice",
        "draft": "DRAFT",
        "issuer": "Issuer / Supplier",
        "client": "Customer",
        "number": "Number",
        "date": "Date",
        "doc_type": "Document type",
        "tax_regime": "Tax regime",
        "sdi_code": "SdI recipient code",
        "pec": "PEC",
        "description": "Description",
        "quantity": "Quantity",
        "unit_price": "Unit price",
        "vat_rate": "VAT rate %",
        "vat_nature": "Nature",
        "total": "Total",
        "taxable": "Taxable",
        "vat": "VAT",
        "stamp_duty": "Stamp duty",
        "doc_total": "Document total",
        "payment": "Payment",
        "method": "Method",
        "iban": "IBAN",
        "terms_days": "Net days",
        "due_date": "Due date",
    },
    "de": {
        "invoice": "Rechnung",
        "draft": "ENTWURF",
        "issuer": "Rechnungssteller",
        "client": "Rechnungsempfänger",
        "number": "Nummer",
        "date": "Datum",
        "doc_type": "Belegart",
        "tax_regime": "Steuerregime",
        "sdi_code": "SdI-Empfängercode",
        "pec": "PEC",
        "description": "Beschreibung",
        "quantity": "Menge",
        "unit_price": "Einzelpreis",
        "vat_rate": "MwSt.-Satz %",
        "vat_nature": "Art",
        "total": "Summe",
        "taxable": "Steuerbasis",
        "vat": "MwSt.",
        "stamp_duty": "Stempelsteuer",
        "doc_total": "Belegsumme",
        "payment": "Zahlung",
        "method": "Methode",
        "iban": "IBAN",
        "terms_days": "Zahlungsfrist (Tage)",
        "due_date": "Fälligkeit",
    },
    "fr": {
        "invoice": "Facture",
        "draft": "BROUILLON",
        "issuer": "Émetteur",
        "client": "Client",
        "number": "Numéro",
        "date": "Date",
        "doc_type": "Type de document",
        "tax_regime": "Régime fiscal",
        "sdi_code": "Code destinataire SdI",
        "pec": "PEC",
        "description": "Description",
        "quantity": "Quantité",
        "unit_price": "Prix unitaire",
        "vat_rate": "Taux TVA %",
        "vat_nature": "Nature",
        "total": "Total",
        "taxable": "Base imposable",
        "vat": "TVA",
        "stamp_duty": "Droit de timbre",
        "doc_total": "Total du document",
        "payment": "Paiement",
        "method": "Mode",
        "iban": "IBAN",
        "terms_days": "Jours net",
        "due_date": "Échéance",
    },
    "es": {
        "invoice": "Factura",
        "draft": "BORRADOR",
        "issuer": "Emisor",
        "client": "Cliente",
        "number": "Número",
        "date": "Fecha",
        "doc_type": "Tipo de documento",
        "tax_regime": "Régimen fiscal",
        "sdi_code": "Código destinatario SdI",
        "pec": "PEC",
        "description": "Descripción",
        "quantity": "Cantidad",
        "unit_price": "Precio unitario",
        "vat_rate": "Tipo IVA %",
        "vat_nature": "Naturaleza",
        "total": "Total",
        "taxable": "Base imponible",
        "vat": "IVA",
        "stamp_duty": "Impuesto de timbre",
        "doc_total": "Total documento",
        "payment": "Pago",
        "method": "Modalidad",
        "iban": "IBAN",
        "terms_days": "Días netos",
        "due_date": "Vencimiento",
    },
}

# Descriptive translations for the two legal Italian phrases we may
# print. The Italian wording stays verbatim (legal value); the locale
# text appears between parentheses below it. NOT a legal translation,
# only a courtesy explanation for the foreign reader.
_FORFETTARIO_GLOSS: dict[str, str] = {
    "en": ("Operation under Italian VAT-exempt forfettario regime, Law 190/2014, art. 1 §§ 54-89"),
    "de": (
        "Vorgang im italienischen Pauschalsteuerregime (Forfettario), "
        "Gesetz Nr. 190/2014 Art. 1 §§ 54-89, von der italienischen MwSt. befreit"
    ),
    "fr": (
        "Opération sous régime forfaitaire italien d'exonération de TVA, "
        "Loi 190/2014 art. 1 §§ 54-89"
    ),
    "es": (
        "Operación bajo régimen forfetario italiano de exención de IVA, "
        "Ley 190/2014 art. 1 §§ 54-89"
    ),
}

_BOLLO_GLOSS: dict[str, str] = {
    "en": "Italian virtual stamp duty paid pursuant to MEF Decree of 17 June 2014, art. 6",
    "de": "Italienische virtuelle Stempelsteuer gemäß MEF-Dekret vom 17. Juni 2014, Art. 6",
    "fr": "Droit de timbre virtuel italien acquitté selon le décret MEF du 17 juin 2014, art. 6",
    "es": (
        "Impuesto de timbre virtual italiano abonado conforme al "
        "Decreto MEF de 17 junio 2014, art. 6"
    ),
}


def _locale(client: ClientProfile | None) -> str:
    if client is None:
        return "it"
    raw = (client.invoice_language or "it").strip().lower()
    # BCP47 may be ``en-GB``: take the primary subtag.
    tag = raw.split("-", 1)[0]
    return tag if tag in _LABELS else "it"


def _L(loc: str, key: str) -> str:
    return _LABELS[loc][key]


def _it_money(d: Decimal) -> str:
    """it-IT currency: thousands '.', decimals ',', trailing ' €'
    (e.g. 1.234,56 €). Kept Italian-style across locales: the currency
    is EUR and the document is fiscally an Italian invoice; cross-locale
    money formatting on a forfettario invoice would just confuse the
    reader without changing the number."""
    q = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if q < 0 else ""
    intpart, dec = f"{abs(q):.2f}".split(".")
    grouped = ""
    while len(intpart) > 3:
        grouped = "." + intpart[-3:] + grouped
        intpart = intpart[:-3]
    return f"{sign}{intpart}{grouped},{dec} €"


def _it_qty(d: Decimal) -> str:
    s = f"{d.normalize():f}" if d == d.to_integral() else f"{d}"
    return s.replace(".", ",")


def _addr(
    address: str | None,
    postal_code: str | None,
    city: str | None,
    province: str | None,
    country: str | None,
) -> str:
    line2 = " ".join(
        x for x in (postal_code or "", city or "", f"({province})" if province else "") if x
    ).strip()
    parts = [address or "", line2, country or ""]
    return "<br/>".join(p for p in parts if p)


# Logo box: scaled to fit within this envelope (preserving aspect ratio)
# at the top-left of the letterhead band. A courtesy mark, not a fiscal
# element, so it stays modest.
_LOGO_MAX_W = 58 * mm
_LOGO_MAX_H = 22 * mm


def logo_is_decodable(data: bytes) -> bool:
    """True iff ``data`` is a FULLY decodable raster (header AND pixels).
    ``getSize`` only parses the header; ``getRGBData`` forces the full
    pixel decode, so a truncated/corrupt body is caught here instead of
    later, at ``doc.build`` draw time (which would crash the PDF). Used
    to reject a bad logo at upload AND to gate it again at render."""
    try:
        reader = ImageReader(BytesIO(data))
        if not all(reader.getSize()):
            return False
        reader.getRGBData()
    except Exception:
        return False
    return True


def _logo_image(logo: bytes | None) -> Image | None:
    """A reportlab ``Image`` scaled to fit the logo box, or None if the
    bytes are absent or not a fully decodable raster. Never raises: a
    broken logo must not break the (courtesy) PDF."""
    if not logo or not logo_is_decodable(logo):
        return None
    try:
        iw, ih = ImageReader(BytesIO(logo)).getSize()
        scale = min(_LOGO_MAX_W / iw, _LOGO_MAX_H / ih)
        img = Image(BytesIO(logo), width=iw * scale, height=ih * scale)
        img.hAlign = "LEFT"
    except Exception:
        # Any decode failure -> no logo; a broken image must never break
        # the courtesy PDF.
        return None
    return img


def _letterhead_flow(
    issuer: IssuerProfile | None, logo: bytes | None, base: ParagraphStyle
) -> list[object]:
    """The optional graphic header at the very top of the page: the
    issuer logo (left) and/or the free-text ``letterhead`` block (right),
    followed by a thin rule. Empty list when the issuer set neither."""
    if issuer is None:
        return []
    img = _logo_image(logo)
    text = (issuer.letterhead or "").strip()
    if img is None and not text:
        return []
    lh_style = ParagraphStyle("letterhead", parent=base, fontSize=9, leading=12)
    # Escape XML metacharacters before turning newlines into <br/> so an
    # ampersand or angle bracket in the header cannot break reportlab's
    # mini-markup (or be mis-parsed as a tag).
    para = Paragraph(escape(text).replace("\n", "<br/>"), lh_style) if text else None
    flow: list[object] = []
    if img is not None and para is not None:
        band = Table([[img, para]], colWidths=[60 * mm, 112 * mm])
        band.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        flow.append(band)
    elif img is not None:
        flow.append(img)
    elif para is not None:
        flow.append(para)
    flow.append(Spacer(1, 2 * mm))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#888888")))
    flow.append(Spacer(1, 3 * mm))
    return flow


def build_pdf(
    invoice: Invoice,
    issuer: IssuerProfile | None,
    client: ClientProfile | None,
    lines: Sequence[InvoiceLine],
    totals: Totals,
    *,
    number: str | None = None,
    is_draft: bool = False,
    logo: bytes | None = None,
) -> bytes:
    """Render the courtesy A4 invoice. Tolerant of a still-incomplete
    draft (missing fields render blank) so it can preview a draft.

    ``number`` is the resolved invoice identifier (e.g. ``"CYLOCK-2"``).
    On a transmitted invoice it equals the real allocated value; on a
    draft it is the would-be number (counter+1) so the user always
    sees the prospective code instead of a bare "BOZZA" placeholder.
    ``is_draft`` adds a small DRAFT/BOZZA marker next to it so the
    page is still visibly non-emitted."""
    loc = _locale(client)
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        title=number
        or (f"{invoice.series}-{invoice.number}" if invoice.number is not None else invoice.series),
    )
    ss = getSampleStyleSheet()
    base = ss["Normal"]
    base.fontName = "Helvetica"
    base.fontSize = 9
    small = ParagraphStyle("small", parent=base, fontSize=8, leading=10)
    h_title = ParagraphStyle(
        "h_title", parent=base, fontSize=16, leading=19, fontName="Helvetica-Bold"
    )
    h_sec = ParagraphStyle("h_sec", parent=base, fontSize=9, leading=12, fontName="Helvetica-Bold")
    # Document identifier — sized to match the AdE-rendered invoice
    # ("Numero: CYLOCK-0001" in large bold). The user explicitly cited
    # the AdE PDF as the reference: the invoice code must be readable
    # at a glance from the page header, not buried in a 3-column table.
    h_number = ParagraphStyle(
        "h_number", parent=base, fontSize=14, leading=18, fontName="Helvetica-Bold"
    )
    right = ParagraphStyle("right", parent=base, alignment=2)
    flow: list[object] = []

    is_forf = _is_forfettario(issuer)
    # ``number`` (the would-be code, e.g. "CYLOCK-2") is the value the
    # caller resolved in InvoicePreview. We display it verbatim and add
    # a small DRAFT/BOZZA marker only when the document is not yet
    # transmitted — the user wants to see "which number am I about to
    # emit", a bare "BOZZA" placeholder hides that information.
    if number:
        display_number = number
    elif invoice.number is not None:
        display_number = f"{invoice.series}-{invoice.number}"
    else:
        # Legacy fallback: no preview number passed AND not yet
        # allocated. Show only the sezionale so the placeholder is
        # short and unambiguous.
        display_number = invoice.series
    date_fmt = client.invoice_date_format if client is not None else None
    issued = format_date((invoice.issued_at or dt.datetime.now(tz=dt.UTC)).date(), date_fmt)

    # Optional graphic letterhead (issuer logo + header text) above the
    # "Fattura" title.
    flow.extend(_letterhead_flow(issuer, logo, base))
    flow.append(Paragraph(_L(loc, "invoice"), h_title))
    draft_tag = f"  ({_L(loc, 'draft')})" if is_draft else ""
    flow.append(
        Paragraph(
            f"{_L(loc, 'number')}: {display_number}{draft_tag}  ·  {issued}",
            h_number,
        )
    )
    flow.append(Spacer(1, 4 * mm))

    # --- cedente (issuer) / cessionario (client) side by side ---
    def _party(
        title: str,
        denom: str,
        vat_number: str | None,
        cf: str | None,
        addr_html: str,
        extra: list[str],
    ) -> Table:
        rows = [
            [Paragraph(title, h_sec)],
            [Paragraph(denom or "", base)],
        ]
        if vat_number:
            rows.append([Paragraph(f"P.IVA {vat_number}", small)])
        if cf:
            rows.append([Paragraph(f"C.F. {cf}", small)])
        if addr_html:
            rows.append([Paragraph(addr_html, small)])
        for e in extra:
            rows.append([Paragraph(e, small)])
        t = Table(rows, colWidths=[83 * mm])
        t.setStyle(
            TableStyle(
                [
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 1),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ]
            )
        )
        return t

    issuer_extra: list[str] = []
    if issuer is not None and issuer.tax_regime:
        issuer_extra.append(f"{_L(loc, 'tax_regime')}: {issuer.tax_regime}")
    cedente = _party(
        _L(loc, "issuer"),
        issuer.legal_name if issuer is not None else "",
        issuer.vat_number if issuer is not None else None,
        issuer.tax_code if issuer is not None else None,
        _addr(
            issuer.address if issuer else None,
            issuer.postal_code if issuer else None,
            issuer.city if issuer else None,
            issuer.province if issuer else None,
            issuer.country if issuer else None,
        ),
        issuer_extra,
    )
    client_extra = []
    if client is not None and client.sdi_code:
        client_extra.append(f"{_L(loc, 'sdi_code')}: {client.sdi_code}")
    if client is not None and client.pec:
        client_extra.append(f"{_L(loc, 'pec')}: {client.pec}")
    cessionario = _party(
        _L(loc, "client"),
        client.legal_name if client is not None else "",
        client.vat_number if client is not None else None,
        client.tax_code if client is not None else None,
        _addr(
            client.address if client else None,
            client.postal_code if client else None,
            client.city if client else None,
            client.province if client else None,
            client.country if client else None,
        ),
        client_extra,
    )
    parties = Table([[cedente, cessionario]], colWidths=[88 * mm, 88 * mm])
    parties.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    flow.append(parties)
    flow.append(Spacer(1, 4 * mm))

    # Document type stays as a fiscal footnote here — the Numero and
    # Data are already shown in the prominent header above. Dropping
    # the previous Numero/Data/Tipo 3-column table avoided duplicating
    # the identifier and freed vertical space without losing the
    # TipoDocumento code (TD01/TD06/…) needed for fiscal traceability.
    flow.append(Paragraph(f"{_L(loc, 'doc_type')}: {invoice.document_type.value}", small))
    flow.append(Spacer(1, 4 * mm))

    # --- lines table ---
    header = [
        Paragraph(f"<b>{_L(loc, 'description')}</b>", small),
        Paragraph(f"<b>{_L(loc, 'quantity')}</b>", right),
        Paragraph(f"<b>{_L(loc, 'unit_price')}</b>", right),
        Paragraph(f"<b>{_L(loc, 'vat_rate')}</b>", right),
        Paragraph(f"<b>{_L(loc, 'vat_nature')}</b>", small),
        Paragraph(f"<b>{_L(loc, 'total')}</b>", right),
    ]
    data: list[list[object]] = [header]
    for ln in lines:
        line_total = (ln.quantity * ln.unit_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        data.append(
            [
                Paragraph(ln.description, small),
                Paragraph(_it_qty(ln.quantity), right),
                Paragraph(_it_money(ln.unit_price), right),
                Paragraph(f"{ln.vat_rate:.2f}".replace(".", ","), right),
                Paragraph(ln.vat_nature or "", small),
                Paragraph(_it_money(line_total), right),
            ]
        )
    lines_tbl = Table(
        data,
        colWidths=[62 * mm, 20 * mm, 28 * mm, 24 * mm, 16 * mm, 26 * mm],
        repeatRows=1,
    )
    lines_tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#888888")),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    flow.append(lines_tbl)
    flow.append(Spacer(1, 5 * mm))

    # --- riepilogo ---
    rie_rows: list[list[object]] = [
        [Paragraph(_L(loc, "taxable"), small), Paragraph(_it_money(totals.taxable), right)],
        [Paragraph(_L(loc, "vat"), small), Paragraph(_it_money(totals.vat), right)],
    ]
    if totals.stamp_duty and totals.stamp_duty > 0:
        rie_rows.append(
            [
                Paragraph(_L(loc, "stamp_duty"), small),
                Paragraph(_it_money(totals.stamp_duty), right),
            ]
        )
    rie_rows.append(
        [
            Paragraph(f"<b>{_L(loc, 'doc_total')}</b>", small),
            Paragraph(f"<b>{_it_money(totals.total)}</b>", right),
        ]
    )
    rie = Table(rie_rows, colWidths=[40 * mm, 40 * mm], hAlign="RIGHT")
    rie.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#888888")),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
                ("LINEABOVE", (0, -1), (-1, -1), 0.7, colors.HexColor("#444444")),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    flow.append(rie)
    flow.append(Spacer(1, 6 * mm))

    # --- payment block ---
    resolved = resolve_payment(invoice, client, issuer)
    if (
        invoice.payment_iban
        or invoice.payment_due_date
        or invoice.payment_terms_days is not None
        or invoice.payment_conditions_code
        or invoice.payment_method_code
    ):
        pay_rows: list[list[object]] = [[Paragraph(_L(loc, "payment"), h_sec), Paragraph("", base)]]
        # Modalità: code + SdI short description (always Italian: the
        # MPxx label is taken from the SdI table verbatim, so foreign
        # readers see the official wording paired with the code).
        method_label = MODALITA_PAGAMENTO.get(resolved.modalita, "")
        pay_rows.append(
            [
                Paragraph(_L(loc, "method"), small),
                Paragraph(f"{resolved.modalita} - {method_label}", small),
            ]
        )
        if invoice.payment_iban:
            pay_rows.append(
                [Paragraph(_L(loc, "iban"), small), Paragraph(invoice.payment_iban, small)]
            )
        if resolved.terms_days is not None:
            pay_rows.append(
                [
                    Paragraph(_L(loc, "terms_days"), small),
                    Paragraph(str(resolved.terms_days), small),
                ]
            )
        if invoice.payment_due_date is not None:
            pay_rows.append(
                [
                    Paragraph(_L(loc, "due_date"), small),
                    Paragraph(format_date(invoice.payment_due_date, date_fmt), small),
                ]
            )
        pay = Table(pay_rows, colWidths=[30 * mm, 90 * mm], hAlign="LEFT")
        pay.setStyle(
            TableStyle(
                [
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 1),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ]
            )
        )
        flow.append(pay)
        flow.append(Spacer(1, 5 * mm))

    # --- forfettario diciture (Italian verbatim + foreign-locale gloss) ---
    # The Italian wording carries the legal value (it identifies the
    # specific Italian statute); the parenthetical gloss is a courtesy
    # explanation for a non-Italian reader and is never relied upon
    # legally. When locale == "it" no gloss is printed.
    if invoice.purpose:
        flow.append(Paragraph(invoice.purpose, small))
        if loc != "it" and invoice.purpose.strip() == FORFETTARIO_CAUSALE:
            gloss = _FORFETTARIO_GLOSS.get(loc)
            if gloss:
                flow.append(Paragraph(f"<i>({gloss})</i>", small))
    if is_forf and totals.stamp_duty and totals.stamp_duty > 0:
        flow.append(Spacer(1, 1 * mm))
        flow.append(Paragraph(BOLLO_DICITURA, small))
        if loc != "it":
            gloss = _BOLLO_GLOSS.get(loc)
            if gloss:
                flow.append(Paragraph(f"<i>({gloss})</i>", small))
    if invoice.notes:
        flow.append(Spacer(1, 2 * mm))
        flow.append(Paragraph(invoice.notes, small))

    doc.build(flow)
    return buf.getvalue()
