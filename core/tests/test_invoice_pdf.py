"""Courtesy-PDF rendering: the civic number joins the street, and the
cedente contact toggles gate what prints. Hermetic (no DB): ``_addr`` is a
pure string helper and ``build_pdf`` takes already-loaded ORM objects.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from mycelium_core.models.client_profile import ClientProfile
from mycelium_core.models.invoice import DocumentType, Invoice, InvoiceLine, IssuerProfile
from mycelium_core.services.invoice_format import Totals
from mycelium_core.services.invoice_pdf import _addr, build_pdf


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
