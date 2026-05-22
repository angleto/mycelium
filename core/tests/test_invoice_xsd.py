"""Official FatturaPA XSD validation (FR-9 hardening, ADR-0011).

Hermetic (no DB, no network): builds the XML in-memory via the same
``_build_xml`` the service uses and validates it against the vendored
official schema. Guards that a well-formed document validates and that
non-conformant input is reported (so transmit can block before SdI).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from flow_core.models.client_profile import ClientProfile
from flow_core.models.invoice import DocumentType, Invoice, InvoiceLine, IssuerProfile
from flow_core.services.invoice_format import _build_xml
from flow_core.services.invoice_xsd import validate_fatturapa


def _valid_xml() -> str:
    issuer = IssuerProfile(
        paese="IT",
        piva="01234567890",
        codice_fiscale=None,
        denominazione="Acme Srl",
        regime_fiscale="RF01",
        indirizzo="Via Roma 1",
        cap="00100",
        comune="Roma",
        provincia="RM",
        nazione="IT",
    )
    client = ClientProfile(
        ragione_sociale="Client SpA",
        id_paese="IT",
        id_codice="09876543210",
        codice_fiscale=None,
        codice_destinatario="ABCDEFG",
        pec=None,
        indirizzo="Via Milano 2",
        cap="20100",
        comune="Milano",
        provincia="MI",
        nazione="IT",
    )
    invoice = Invoice(
        document_type=DocumentType.TD01,
        currency="EUR",
        issued_at=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
        series="A",
        number=1,
        taxable=Decimal("200.00"),
        vat=Decimal("44.00"),
        bollo=Decimal("0.00"),
        total=Decimal("244.00"),
        causale=None,
        notes=None,
        payment_iban=None,
        payment_due_date=None,
    )
    line = InvoiceLine(
        line_no=1,
        description="consulting",
        quantity=Decimal(2),
        unit_price=Decimal("100.00"),
        vat_rate=Decimal(22),
        natura=None,
    )
    return _build_xml(invoice, issuer, client, [line], "202600001")


def test_valid_fatturapa_passes_xsd() -> None:
    assert validate_fatturapa(_valid_xml()) == []


def test_malformed_xml_is_reported() -> None:
    assert validate_fatturapa("<not-well-formed")


def test_non_fatturapa_root_is_rejected() -> None:
    # A well-formed but wrong-root document must not validate.
    assert validate_fatturapa("<foo/>")
