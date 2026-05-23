"""Human-readable A4 invoice PDF (the courtesy copy, not the legal
document: the legal document is the FatturaPA XML transited via SdI).

Pure-Python (reportlab built-in fonts only, no external font files).
The forfettario diciture (L.190/2014 causale, and the virtual-stamp
note when bollo applies) are printed verbatim in Italian and followed
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

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from flow_core.models.client_profile import ClientProfile
from flow_core.models.invoice import Invoice, InvoiceLine, IssuerProfile
from flow_core.services.invoice_format import (
    BOLLO_DICITURA,
    FORFETTARIO_CAUSALE,
    Totals,
    _is_forfettario,
)
from flow_core.services.payment_methods import (
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
        "regime_fiscale": "Regime fiscale",
        "codice_destinatario": "Codice Destinatario",
        "pec": "PEC",
        "description": "Descrizione",
        "quantity": "Quantità",
        "unit_price": "Prezzo unitario",
        "vat_rate": "Aliquota IVA %",
        "natura": "Natura",
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
        "regime_fiscale": "Tax regime",
        "codice_destinatario": "SdI recipient code",
        "pec": "PEC",
        "description": "Description",
        "quantity": "Quantity",
        "unit_price": "Unit price",
        "vat_rate": "VAT rate %",
        "natura": "Nature",
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
        "regime_fiscale": "Steuerregime",
        "codice_destinatario": "SdI-Empfängercode",
        "pec": "PEC",
        "description": "Beschreibung",
        "quantity": "Menge",
        "unit_price": "Einzelpreis",
        "vat_rate": "MwSt.-Satz %",
        "natura": "Art",
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
        "regime_fiscale": "Régime fiscal",
        "codice_destinatario": "Code destinataire SdI",
        "pec": "PEC",
        "description": "Description",
        "quantity": "Quantité",
        "unit_price": "Prix unitaire",
        "vat_rate": "Taux TVA %",
        "natura": "Nature",
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
        "regime_fiscale": "Régimen fiscal",
        "codice_destinatario": "Código destinatario SdI",
        "pec": "PEC",
        "description": "Descripción",
        "quantity": "Cantidad",
        "unit_price": "Precio unitario",
        "vat_rate": "Tipo IVA %",
        "natura": "Naturaleza",
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
    indirizzo: str | None,
    cap: str | None,
    comune: str | None,
    provincia: str | None,
    nazione: str | None,
) -> str:
    line2 = " ".join(
        x for x in (cap or "", comune or "", f"({provincia})" if provincia else "") if x
    ).strip()
    parts = [indirizzo or "", line2, nazione or ""]
    return "<br/>".join(p for p in parts if p)


def build_pdf(
    invoice: Invoice,
    issuer: IssuerProfile | None,
    client: ClientProfile | None,
    lines: Sequence[InvoiceLine],
    totals: Totals,
) -> bytes:
    """Render the courtesy A4 invoice. Tolerant of a still-incomplete
    draft (missing fields render blank) so it can preview a draft."""
    loc = _locale(client)
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        title=f"{invoice.series}{invoice.number or ''}",
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
    right = ParagraphStyle("right", parent=base, alignment=2)
    flow: list[object] = []

    is_forf = _is_forfettario(issuer)
    number = f"{invoice.series}{invoice.number}" if invoice.number is not None else _L(loc, "draft")
    issued = (invoice.issued_at or dt.datetime.now(tz=dt.UTC)).date().isoformat()

    flow.append(Paragraph(_L(loc, "invoice"), h_title))
    flow.append(Spacer(1, 2 * mm))

    # --- cedente (issuer) / cessionario (client) side by side ---
    def _party(
        title: str,
        denom: str,
        piva: str | None,
        cf: str | None,
        addr_html: str,
        extra: list[str],
    ) -> Table:
        rows = [
            [Paragraph(title, h_sec)],
            [Paragraph(denom or "", base)],
        ]
        if piva:
            rows.append([Paragraph(f"P.IVA {piva}", small)])
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
    if issuer is not None and issuer.regime_fiscale:
        issuer_extra.append(f"{_L(loc, 'regime_fiscale')}: {issuer.regime_fiscale}")
    cedente = _party(
        _L(loc, "issuer"),
        issuer.denominazione if issuer is not None else "",
        issuer.piva if issuer is not None else None,
        issuer.codice_fiscale if issuer is not None else None,
        _addr(
            issuer.indirizzo if issuer else None,
            issuer.cap if issuer else None,
            issuer.comune if issuer else None,
            issuer.provincia if issuer else None,
            issuer.nazione if issuer else None,
        ),
        issuer_extra,
    )
    client_extra = []
    if client is not None and client.codice_destinatario:
        client_extra.append(f"{_L(loc, 'codice_destinatario')}: {client.codice_destinatario}")
    if client is not None and client.pec:
        client_extra.append(f"{_L(loc, 'pec')}: {client.pec}")
    cessionario = _party(
        _L(loc, "client"),
        client.ragione_sociale if client is not None else "",
        client.id_codice if client is not None else None,
        client.codice_fiscale if client is not None else None,
        _addr(
            client.indirizzo if client else None,
            client.cap if client else None,
            client.comune if client else None,
            client.provincia if client else None,
            client.nazione if client else None,
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
    flow.append(Spacer(1, 6 * mm))

    # --- document header (Numero / Data / Tipo) ---
    doc_tbl = Table(
        [
            [
                Paragraph(_L(loc, "number"), h_sec),
                Paragraph(_L(loc, "date"), h_sec),
                Paragraph(_L(loc, "doc_type"), h_sec),
            ],
            [
                Paragraph(number, base),
                Paragraph(issued, base),
                Paragraph(invoice.document_type.value, base),
            ],
        ],
        colWidths=[58 * mm, 58 * mm, 58 * mm],
    )
    doc_tbl.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#888888")),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f0f0")),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    flow.append(doc_tbl)
    flow.append(Spacer(1, 6 * mm))

    # --- lines table ---
    header = [
        Paragraph(f"<b>{_L(loc, 'description')}</b>", small),
        Paragraph(f"<b>{_L(loc, 'quantity')}</b>", right),
        Paragraph(f"<b>{_L(loc, 'unit_price')}</b>", right),
        Paragraph(f"<b>{_L(loc, 'vat_rate')}</b>", right),
        Paragraph(f"<b>{_L(loc, 'natura')}</b>", small),
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
                Paragraph(ln.natura or "", small),
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
    if totals.bollo and totals.bollo > 0:
        rie_rows.append(
            [
                Paragraph(_L(loc, "stamp_duty"), small),
                Paragraph(_it_money(totals.bollo), right),
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
        or invoice.condizioni_pagamento
        or invoice.modalita_pagamento
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
                    Paragraph(invoice.payment_due_date.isoformat(), small),
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
    if invoice.causale:
        flow.append(Paragraph(invoice.causale, small))
        if loc != "it" and invoice.causale.strip() == FORFETTARIO_CAUSALE:
            gloss = _FORFETTARIO_GLOSS.get(loc)
            if gloss:
                flow.append(Paragraph(f"<i>({gloss})</i>", small))
    if is_forf and totals.bollo and totals.bollo > 0:
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
