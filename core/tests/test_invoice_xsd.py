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
from flow_core.services.invoice_format import FORFETTARIO_RIFERIMENTO_NORMATIVO, _build_xml
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


def _forfettario_xml(riferimento: str | None = None) -> str:
    issuer = IssuerProfile(
        paese="IT",
        piva="13438810015",
        codice_fiscale="LTENGL79M31I356X",
        denominazione="Angelo Leto",
        regime_fiscale="RF19",
        indirizzo="Via Roma 1",
        cap="00100",
        comune="Roma",
        provincia="RM",
        nazione="IT",
        riferimento_normativo=riferimento,
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
        number=2,
        taxable=Decimal("200.00"),
        vat=Decimal("0.00"),
        bollo=Decimal("2.00"),
        total=Decimal("202.00"),
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
        vat_rate=Decimal(0),
        natura="N2.2",
    )
    return _build_xml(invoice, issuer, client, [line], "202600002")


def test_forfettario_emits_default_riferimento_normativo() -> None:
    xml = _forfettario_xml()
    assert validate_fatturapa(xml) == []
    assert (
        f"<RiferimentoNormativo>{FORFETTARIO_RIFERIMENTO_NORMATIVO}</RiferimentoNormativo>" in xml
    )


def test_issuer_riferimento_normativo_overrides_default() -> None:
    custom = "Art. 1 c.54-89 L.190/2014 - regime forfettario"
    xml = _forfettario_xml(custom)
    assert validate_fatturapa(xml) == []
    assert f"<RiferimentoNormativo>{custom}</RiferimentoNormativo>" in xml
    assert FORFETTARIO_RIFERIMENTO_NORMATIVO not in xml


def test_persona_fisica_emits_nome_cognome() -> None:
    issuer = IssuerProfile(
        paese="IT",
        piva="13438810015",
        codice_fiscale="LTENGL79M31I356X",
        denominazione="Angelo Leto",
        regime_fiscale="RF01",
        indirizzo="Via Roma 1",
        cap="00100",
        comune="Roma",
        provincia="RM",
        nazione="IT",
        nome="Angelo",
        cognome="Leto",
    )
    client = ClientProfile(
        ragione_sociale="Mario Rossi",
        id_paese="IT",
        id_codice=None,
        codice_fiscale="RSSMRA80A01H501U",
        codice_destinatario="ABCDEFG",
        pec=None,
        indirizzo="Via Milano 2",
        cap="20100",
        comune="Milano",
        provincia="MI",
        nazione="IT",
        nome="Mario",
        cognome="Rossi",
    )
    invoice = Invoice(
        document_type=DocumentType.TD01,
        currency="EUR",
        issued_at=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
        series="A",
        number=3,
        taxable=Decimal("100.00"),
        vat=Decimal("22.00"),
        bollo=Decimal("0.00"),
        total=Decimal("122.00"),
        causale=None,
        notes=None,
        payment_iban=None,
        payment_due_date=None,
    )
    line = InvoiceLine(
        line_no=1,
        description="consulting",
        quantity=Decimal(1),
        unit_price=Decimal("100.00"),
        vat_rate=Decimal(22),
        natura=None,
    )
    xml = _build_xml(invoice, issuer, client, [line], "202600003")
    assert validate_fatturapa(xml) == []
    # Both cedente and cessionario use Anagrafica/Nome+Cognome, not Denominazione.
    assert "<Nome>Angelo</Nome><Cognome>Leto</Cognome>" in xml
    assert "<Nome>Mario</Nome><Cognome>Rossi</Cognome>" in xml
    assert "<Denominazione>" not in xml


def test_recipient_pec_goes_in_pecdestinatario() -> None:
    issuer = IssuerProfile(
        paese="IT",
        piva="13438810015",
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
        ragione_sociale="Mario Rossi",
        id_paese="IT",
        id_codice=None,
        codice_fiscale="RSSMRA80A01H501U",
        codice_destinatario=None,  # no SdI code: delivery is by PEC
        pec="mario@pec.example.it",
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
        number=4,
        taxable=Decimal("100.00"),
        vat=Decimal("22.00"),
        bollo=Decimal("0.00"),
        total=Decimal("122.00"),
        causale=None,
        notes=None,
        payment_iban=None,
        payment_due_date=None,
    )
    line = InvoiceLine(
        line_no=1,
        description="consulting",
        quantity=Decimal(1),
        unit_price=Decimal("100.00"),
        vat_rate=Decimal(22),
        natura=None,
    )
    xml = _build_xml(invoice, issuer, client, [line], "202600004")
    assert validate_fatturapa(xml) == []
    assert "<CodiceDestinatario>0000000</CodiceDestinatario>" in xml
    # The recipient PEC is the routing address (PECDestinatario), not the
    # transmitter's ContattiTrasmittente/Email.
    assert "<PECDestinatario>mario@pec.example.it</PECDestinatario>" in xml
    assert "<ContattiTrasmittente>" not in xml
