"""Courtesy-PDF rendering: the civic number joins the street, and the
cedente contact toggles gate what prints. Hermetic (no DB): ``_addr`` is a
pure string helper and ``build_pdf`` takes already-loaded ORM objects.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from reportlab import rl_config
from reportlab.lib.styles import ParagraphStyle

from mycelium_core.models.client_profile import ClientProfile
from mycelium_core.models.invoice import (
    DocumentType,
    Invoice,
    InvoiceLine,
    InvoiceLineAltriDati,
    IssuerProfile,
)
from mycelium_core.services.invoice_format import Totals
from mycelium_core.services.invoice_pdf import (
    _addr,
    _altri_dati_table,
    _it_money,
    _it_unit_price,
    build_pdf,
)


def test_addr_appends_civic_number() -> None:
    # The dedicated NumeroCivico joins the street ("Via X, 77"); absent, the
    # street stands alone (the number may live inline already).
    assert _addr("via Giuseppe Verdi", "77", "10154", "Torino", "TO", "IT") == (
        "via Giuseppe Verdi, 77<br/>10154 Torino (TO)<br/>IT"
    )
    assert _addr("via Giuseppe Verdi 77", None, "10154", "Torino", "TO", "IT") == (
        "via Giuseppe Verdi 77<br/>10154 Torino (TO)<br/>IT"
    )
    # An empty street with only a civic number still renders the number.
    assert _addr("", "12/A", "20100", "Milano", "MI", "IT").startswith("12/A<br/>")


def _issuer(**over: object) -> IssuerProfile:
    base: dict[str, object] = dict(
        country_code="IT",
        vat_number="01112223334",
        tax_code="RSSMRA80A01H501U",
        legal_name="Mario Rossi",
        tax_regime="RF19",
        address="via Giuseppe Verdi",
        civic_number="77",
        postal_code="10154",
        city="Torino",
        province="TO",
        country="IT",
        phone="+39 333 1234567",
        email="info@acme.it",
        pec="acme@pec.it",
    )
    base.update(over)
    return IssuerProfile(**base)


def _doc() -> tuple[Invoice, ClientProfile, list[InvoiceLine], Totals]:
    client = ClientProfile(
        legal_name="Client SpA",
        country_code="IT",
        vat_number="09876543210",
        tax_code=None,
        sdi_code="ABCDEFG",
        pec=None,
        address="Via Milano",
        civic_number="12/A",
        postal_code="20100",
        city="Milano",
        province="MI",
        country="IT",
    )
    inv = Invoice(
        document_type=DocumentType.TD01,
        currency="EUR",
        issued_at=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
        series="A",
        number=1,
        taxable=Decimal("200.00"),
        vat=Decimal("0.00"),
        stamp_duty=Decimal("2.00"),
        total=Decimal("202.00"),
        purpose=None,
        notes=None,
        payment_iban=None,
        payment_due_date=None,
    )
    line = InvoiceLine(
        # An explicit id: the AltriDatiGestionali mapping is keyed by it
        # (a transient ORM object has none until flush).
        id=uuid.uuid4(),
        line_no=1,
        description="consulting",
        quantity=Decimal(2),
        unit_price=Decimal("100.00"),
        vat_rate=Decimal(0),
        vat_nature="N2.2",
    )
    totals = Totals(
        taxable=Decimal("200.00"),
        vat=Decimal("0.00"),
        stamp_duty=Decimal("2.00"),
        total=Decimal("202.00"),
    )
    return inv, client, [line], totals


def test_build_pdf_renders_with_civic_and_contacts() -> None:
    inv, client, lines, totals = _doc()
    pdf = build_pdf(
        inv,
        _issuer(show_phone=True, show_email=True, show_pec=False),
        client,
        lines,
        totals,
        number="A-1",
        is_draft=False,
        logo=None,
    )
    assert pdf[:4] == b"%PDF" and len(pdf) > 1000


def test_build_pdf_renders_with_all_contacts_hidden() -> None:
    # Hiding every contact must not break the layout (empty extra rows).
    inv, client, lines, totals = _doc()
    pdf = build_pdf(
        inv,
        _issuer(show_phone=False, show_email=False, show_pec=False),
        client,
        lines,
        totals,
        number="A-1",
        is_draft=True,
        logo=None,
    )
    assert pdf[:4] == b"%PDF"


# --- AltriDatiGestionali on the courtesy PDF (FatturaPA 2.2.1.16) ---


def _adg(
    line_id: uuid.UUID,
    ord_: int,
    tipo: str,
    *,
    testo: str | None = None,
    numero: Decimal | None = None,
    data: dt.date | None = None,
) -> InvoiceLineAltriDati:
    return InvoiceLineAltriDati(
        invoice_line_id=line_id,
        ord=ord_,
        tipo_dato=tipo,
        riferimento_testo=testo,
        riferimento_numero=numero,
        riferimento_data=data,
    )


def _adg_line(line_no: int, line_id: uuid.UUID) -> InvoiceLine:
    """Minimal line: the standalone table only reads id and line_no."""
    return InvoiceLine(
        id=line_id,
        invoice_id=uuid.uuid4(),
        line_no=line_no,
        description="d",
        quantity=Decimal("1"),
        unit_price=Decimal("1"),
        vat_rate=Decimal("22"),
    )


def _adg_cells(
    blocks: list[InvoiceLineAltriDati],
    loc: str = "it",
    date_fmt: str | None = None,
    line_no: int = 1,
) -> list[list[str]]:
    """The rendered text of every cell of the standalone table, header
    included, asserted directly: cheaper and far more precise than
    digging text out of a compressed PDF content stream."""
    line_id = blocks[0].invoice_line_id if blocks else uuid.uuid4()
    small = ParagraphStyle("s", fontName="Helvetica", fontSize=7)
    right = ParagraphStyle("r", parent=small, alignment=2)
    tbl = _altri_dati_table(
        [_adg_line(line_no, line_id)], {line_id: blocks}, loc, small, right, date_fmt
    )
    assert tbl is not None
    # str(): reportlab ships no type information, so ``.text`` is Any.
    return [[str(c.text) for c in row] for row in tbl._cellvalues]


def test_altri_dati_table_lists_blocks_in_ord_order() -> None:
    line_id = uuid.uuid4()
    blocks = [
        # reversed on purpose: the table sorts by ``ord``, like the XML
        _adg(line_id, 1, "N.DOC.COMM", testo="0001", numero=Decimal("7"), data=dt.date(2026, 2, 2)),
        _adg(line_id, 0, "INTENTO", testo="12345/2026-1"),
    ]
    rows = _adg_cells(blocks, "it", "DD/MM/YYYY", line_no=3)
    assert rows[1] == ["3", "INTENTO", "12345/2026-1", "", ""]
    assert rows[2] == ["3", "N.DOC.COMM", "0001", "7,00", "02/02/2026"]


def test_altri_dati_table_skips_empty_optional_fields() -> None:
    # NB3 carries only its TipoDato: the three optional columns stay
    # empty rather than printing a placeholder.
    line_id = uuid.uuid4()
    rows = _adg_cells([_adg(line_id, 0, "NB3")])
    assert rows[1] == ["1", "NB3", "", "", ""]


def test_altri_dati_table_headers_are_localised() -> None:
    # Column labels come from the per-locale dict, never hardcoded.
    line_id = uuid.uuid4()
    blocks = [_adg(line_id, 0, "NB3")]
    assert _adg_cells(blocks, "it")[0][1] == "<b>Tipo dato</b>"
    assert _adg_cells(blocks, "en")[0][1] == "<b>Data type</b>"
    assert _adg_cells(blocks, "de")[0][1] == "<b>Datenart</b>"
    assert _adg_cells(blocks, "fr")[0][1] == "<b>Type de donnée</b>"
    assert _adg_cells(blocks, "es")[0][1] == "<b>Tipo de dato</b>"


def test_altri_dati_table_escapes_user_text() -> None:
    # tipo_dato / riferimento_testo are user values and reportlab reads
    # mini-markup: an angle bracket must not become a tag.
    rows = _adg_cells([_adg(uuid.uuid4(), 0, "A&B", testo="x <b>y</b>")])
    assert rows[1][1] == "A&amp;B"
    assert rows[1][2] == "x &lt;b&gt;y&lt;/b&gt;"


def test_altri_dati_table_is_absent_when_no_line_carries_blocks() -> None:
    # The ordinary invoice must render exactly as before this existed.
    small = ParagraphStyle("s", fontName="Helvetica", fontSize=7)
    right = ParagraphStyle("r", parent=small, alignment=2)
    assert _altri_dati_table([_adg_line(1, uuid.uuid4())], {}, "it", small, right, None) is None


def test_build_pdf_renders_altri_dati_blocks() -> None:
    inv, client, lines, totals = _doc()
    line_id = lines[0].id
    pdf = build_pdf(
        inv,
        _issuer(),
        client,
        lines,
        totals,
        number="A-1",
        is_draft=False,
        logo=None,
        altri_dati={
            line_id: [
                _adg(line_id, 0, "INTENTO", testo="12345/2026-1"),
                _adg(line_id, 1, "NB3"),
            ]
        },
    )
    assert pdf[:4] == b"%PDF" and len(pdf) > 1000
    # The blocks add ink: the same document without them is smaller.
    plain = build_pdf(
        inv, _issuer(), client, lines, totals, number="A-1", is_draft=False, logo=None
    )
    assert len(pdf) > len(plain)


def test_build_pdf_without_blocks_is_byte_identical() -> None:
    # A line with no block must render exactly as before. reportlab
    # stamps a creation time and a doc id, so determinism needs
    # rl_config.invariant; with it on, the three no-block spellings
    # (absent / empty mapping / another line's id) are the same bytes.
    inv, client, lines, totals = _doc()
    other = uuid.uuid4()
    previous = rl_config.invariant
    rl_config.invariant = 1
    try:
        base = build_pdf(
            inv, _issuer(), client, lines, totals, number="A-1", is_draft=False, logo=None
        )
        variants = [
            build_pdf(
                inv,
                _issuer(),
                client,
                lines,
                totals,
                number="A-1",
                is_draft=False,
                logo=None,
                altri_dati=altri,
            )
            for altri in (None, {}, {other: [_adg(other, 0, "INTENTO", testo="x")]})
        ]
    finally:
        rl_config.invariant = previous
    for v in variants:
        assert v == base


def test_unit_price_prints_stored_precision_only_when_it_matters() -> None:
    # unit_price is Numeric(14,4). Printing it at 2 decimals next to a
    # line total computed from all 4 made the courtesy copy contradict
    # itself (2 x 62,54 = 125,08, not 125,09) -- the same defect the XML
    # PrezzoUnitario had. Values with no significant 3rd/4th decimal
    # print exactly as before.
    assert _it_unit_price(Decimal("100.0000")) == "100,00 €"
    assert _it_unit_price(Decimal("1234.5000")) == "1.234,50 €"
    assert _it_unit_price(Decimal("-100.00")) == "-100,00 €"
    assert _it_unit_price(Decimal("62.5432")) == "62,5432 €"
    assert _it_unit_price(Decimal("62.5430")) == "62,543 €"
    assert _it_unit_price(Decimal("1234.5678")) == "1.234,5678 €"
    # Same value, same string as the plain money formatter when 2 decimals
    # are enough: the default invoice's bytes are untouched.
    for v in ("0.00", "9.99", "1234.50", "-7.25"):
        assert _it_unit_price(Decimal(v)) == _it_money(Decimal(v))
