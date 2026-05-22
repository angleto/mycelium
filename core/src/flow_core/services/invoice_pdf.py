"""Human-readable A4 invoice PDF (the courtesy copy, not the legal
document: the legal document is the FatturaPA XML transited via SdI).

Pure-Python (reportlab built-in fonts only, no external font files).
The forfettario diciture (L.190/2014 causale, and the virtual-stamp
note when bollo applies) are printed verbatim, mirroring the XML. The
service builds the totals; this module only renders them.
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
    Totals,
    _is_forfettario,
)


def _it_money(d: Decimal) -> str:
    """it-IT currency: thousands '.', decimals ',', trailing ' €'
    (e.g. 1.234,56 €)."""
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
    number = f"{invoice.series}{invoice.number}" if invoice.number is not None else "BOZZA"
    issued = (invoice.issued_at or dt.datetime.now(tz=dt.UTC)).date().isoformat()

    flow.append(Paragraph("Fattura", h_title))
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
        issuer_extra.append(f"Regime fiscale: {issuer.regime_fiscale}")
    cedente = _party(
        "Cedente / Prestatore",
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
        client_extra.append(f"Codice Destinatario: {client.codice_destinatario}")
    if client is not None and client.pec:
        client_extra.append(f"PEC: {client.pec}")
    cessionario = _party(
        "Cessionario / Committente",
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
                Paragraph("Numero", h_sec),
                Paragraph("Data", h_sec),
                Paragraph("Tipo documento", h_sec),
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
        Paragraph("<b>Descrizione</b>", small),
        Paragraph("<b>Quantità</b>", right),
        Paragraph("<b>Prezzo unitario</b>", right),
        Paragraph("<b>Aliquota IVA %</b>", right),
        Paragraph("<b>Natura</b>", small),
        Paragraph("<b>Totale</b>", right),
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
        [Paragraph("Imponibile", small), Paragraph(_it_money(totals.taxable), right)],
        [Paragraph("IVA", small), Paragraph(_it_money(totals.vat), right)],
    ]
    if totals.bollo and totals.bollo > 0:
        rie_rows.append(
            [
                Paragraph("Imposta di bollo", small),
                Paragraph(_it_money(totals.bollo), right),
            ]
        )
    rie_rows.append(
        [
            Paragraph("<b>Totale documento</b>", small),
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
    if invoice.payment_iban or invoice.payment_due_date:
        pay_rows: list[list[object]] = [[Paragraph("Pagamento", h_sec), Paragraph("", base)]]
        pay_rows.append([Paragraph("Modalità", small), Paragraph("MP05 - Bonifico", small)])
        if invoice.payment_iban:
            pay_rows.append([Paragraph("IBAN", small), Paragraph(invoice.payment_iban, small)])
        if invoice.payment_due_date is not None:
            pay_rows.append(
                [
                    Paragraph("Scadenza", small),
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

    # --- forfettario diciture (verbatim) ---
    if invoice.causale:
        flow.append(Paragraph(invoice.causale, small))
    if is_forf and totals.bollo and totals.bollo > 0:
        flow.append(Spacer(1, 1 * mm))
        flow.append(Paragraph(BOLLO_DICITURA, small))
    if invoice.notes:
        flow.append(Spacer(1, 2 * mm))
        flow.append(Paragraph(invoice.notes, small))

    doc.build(flow)
    return buf.getvalue()
