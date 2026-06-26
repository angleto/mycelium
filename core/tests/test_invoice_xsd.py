"""Official FatturaPA XSD validation (FR-9 hardening, ADR-0011).

Hermetic (no DB, no network): builds the XML in-memory via the same
``_build_xml`` the service uses and validates it against the vendored
official schema. Guards that a well-formed document validates and that
non-conformant input is reported (so transmit can block before SdI).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from mycelium_core.models.client_profile import ClientProfile
from mycelium_core.models.invoice import DocumentType, Invoice, InvoiceLine, IssuerProfile
from mycelium_core.services.invoice_format import (
    FORFETTARIO_RIFERIMENTO_NORMATIVO,
    _bare_id_codice,
    _build_xml,
)
from mycelium_core.services.invoice_xsd import validate_fatturapa


def _valid_xml() -> str:
    issuer = IssuerProfile(
        country_code="IT",
        vat_number="01234567890",
        tax_code=None,
        legal_name="Acme Srl",
        tax_regime="RF01",
        address="Via Roma 1",
        postal_code="00100",
        city="Roma",
        province="RM",
        country="IT",
    )
    client = ClientProfile(
        legal_name="Client SpA",
        country_code="IT",
        vat_number="09876543210",
        tax_code=None,
        sdi_code="ABCDEFG",
        pec=None,
        address="Via Milano 2",
        postal_code="20100",
        city="Milano",
        province="MI",
        country="IT",
    )
    invoice = Invoice(
        document_type=DocumentType.TD01,
        currency="EUR",
        issued_at=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
        series="A",
        number=1,
        taxable=Decimal("200.00"),
        vat=Decimal("44.00"),
        stamp_duty=Decimal("0.00"),
        total=Decimal("244.00"),
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
        vat_rate=Decimal(22),
        vat_nature=None,
    )
    return _build_xml(invoice, issuer, client, [line], "202600001")


def test_valid_fatturapa_passes_xsd() -> None:
    assert validate_fatturapa(_valid_xml()) == []


def test_fpa12_for_six_char_codice_validates() -> None:
    # A 6-char CodiceDestinatario (PA codice univoco ufficio) makes the document
    # a B2G FPA12 (versione + FormatoTrasmissione); a 7-char one stays FPR12.
    # The minimal FPA12 is XSD-valid -- CIG/CUP/split-payment are PA-side rifiuto
    # concerns, not SdI scarto, and the interop test does not validate content.
    issuer = IssuerProfile(
        country_code="IT",
        vat_number="13438810015",
        tax_code="LTENGL79M31I356X",
        legal_name="Angelo Leto",
        tax_regime="RF19",
        address="Via Roma 1",
        postal_code="00100",
        city="Roma",
        province="RM",
        country="IT",
    )
    client = ClientProfile(
        legal_name="Ufficio Test PA",
        country_code="IT",
        vat_number="03535510048",
        tax_code="03535510048",
        sdi_code="VRGXZS",
        pec=None,
        address="Via PA 1",
        postal_code="00100",
        city="Roma",
        province="RM",
        country="IT",
    )
    invoice = Invoice(
        document_type=DocumentType.TD01,
        currency="EUR",
        issued_at=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
        series="A",
        number=1,
        taxable=Decimal("100.00"),
        vat=Decimal("0.00"),
        stamp_duty=Decimal("0.00"),
        total=Decimal("100.00"),
        purpose=None,
        notes=None,
        payment_iban=None,
        payment_due_date=None,
    )
    line = InvoiceLine(
        line_no=1,
        description="x",
        quantity=Decimal(1),
        unit_price=Decimal("100.00"),
        vat_rate=Decimal(0),
        vat_nature="N2.2",
    )
    xml = _build_xml(invoice, issuer, client, [line], "202600001")
    assert 'versione="FPA12"' in xml
    assert "<FormatoTrasmissione>FPA12</FormatoTrasmissione>" in xml
    assert "<CodiceDestinatario>VRGXZS</CodiceDestinatario>" in xml
    assert validate_fatturapa(xml) == []


def test_bare_id_codice_strips_redundant_country_prefix() -> None:
    # A VAT stored with a leading country prefix matching IdPaese is emitted
    # bare; a clean VAT and a codice fiscale (3rd char is a letter) are left
    # untouched.
    assert _bare_id_codice("IT13438810015", "IT") == "13438810015"
    assert _bare_id_codice("13438810015", "IT") == "13438810015"
    assert _bare_id_codice("LTENGL79M31I356X", "IT") == "LTENGL79M31I356X"
    assert _bare_id_codice("IT13438810015", "it") == "13438810015"


def test_country_prefixed_piva_emits_bare_idcodice_and_validates() -> None:
    # Regression: an issuer/client whose stored VAT carries the IdPaese prefix
    # (e.g. "IT13438810015") must still emit an 11-digit IdCodice -- the
    # backend never assembles country+number, so SdI accepts the cedente.
    issuer = IssuerProfile(
        country_code="IT",
        vat_number="IT13438810015",
        tax_code="LTENGL79M31I356X",
        legal_name="Angelo Leto",
        tax_regime="RF19",
        address="Via Roma 1",
        postal_code="00100",
        city="Roma",
        province="RM",
        country="IT",
    )
    client = ClientProfile(
        legal_name="Client SpA",
        country_code="IT",
        vat_number="IT09876543210",
        tax_code=None,
        sdi_code="ABCDEFG",
        pec=None,
        address="Via Milano 2",
        postal_code="20100",
        city="Milano",
        province="MI",
        country="IT",
    )
    invoice = Invoice(
        document_type=DocumentType.TD01,
        currency="EUR",
        issued_at=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
        series="A",
        number=1,
        taxable=Decimal("100.00"),
        vat=Decimal("0.00"),
        stamp_duty=Decimal("0.00"),
        total=Decimal("100.00"),
        purpose=None,
        notes=None,
        payment_iban=None,
        payment_due_date=None,
    )
    line = InvoiceLine(
        line_no=1,
        description="consulting",
        quantity=Decimal(1),
        unit_price=Decimal("100.00"),
        vat_rate=Decimal(0),
        vat_nature="N2.2",
    )
    xml = _build_xml(invoice, issuer, client, [line], "202600001")
    assert "<IdCodice>13438810015</IdCodice>" in xml
    assert "<IdCodice>09876543210</IdCodice>" in xml
    assert "IT13438810015" not in xml
    # IdTrasmittente/IdCodice is a CODICE FISCALE for SdI: a physical-person
    # channel holder must transmit under the 16-char CF, not the P.IVA (a P.IVA
    # there is scartata 00300). The cedente IdFiscaleIVA above still carries the
    # P.IVA (13438810015).
    assert (
        "<IdTrasmittente><IdPaese>IT</IdPaese>"
        "<IdCodice>LTENGL79M31I356X</IdCodice></IdTrasmittente>" in xml
    )
    assert validate_fatturapa(xml) == []


def test_malformed_xml_is_reported() -> None:
    assert validate_fatturapa("<not-well-formed")


def test_non_fatturapa_root_is_rejected() -> None:
    # A well-formed but wrong-root document must not validate.
    assert validate_fatturapa("<foo/>")


def _forfettario_xml(riferimento: str | None = None) -> str:
    issuer = IssuerProfile(
        country_code="IT",
        vat_number="13438810015",
        tax_code="LTENGL79M31I356X",
        legal_name="Angelo Leto",
        tax_regime="RF19",
        address="Via Roma 1",
        postal_code="00100",
        city="Roma",
        province="RM",
        country="IT",
        legal_reference=riferimento,
    )
    client = ClientProfile(
        legal_name="Client SpA",
        country_code="IT",
        vat_number="09876543210",
        tax_code=None,
        sdi_code="ABCDEFG",
        pec=None,
        address="Via Milano 2",
        postal_code="20100",
        city="Milano",
        province="MI",
        country="IT",
    )
    invoice = Invoice(
        document_type=DocumentType.TD01,
        currency="EUR",
        issued_at=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
        series="A",
        number=2,
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
        country_code="IT",
        vat_number="13438810015",
        tax_code="LTENGL79M31I356X",
        legal_name="Angelo Leto",
        tax_regime="RF01",
        address="Via Roma 1",
        postal_code="00100",
        city="Roma",
        province="RM",
        country="IT",
        first_name="Angelo",
        last_name="Leto",
    )
    client = ClientProfile(
        legal_name="Mario Rossi",
        country_code="IT",
        vat_number=None,
        tax_code="RSSMRA80A01H501U",
        sdi_code="ABCDEFG",
        pec=None,
        address="Via Milano 2",
        postal_code="20100",
        city="Milano",
        province="MI",
        country="IT",
        first_name="Mario",
        last_name="Rossi",
    )
    invoice = Invoice(
        document_type=DocumentType.TD01,
        currency="EUR",
        issued_at=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
        series="A",
        number=3,
        taxable=Decimal("100.00"),
        vat=Decimal("22.00"),
        stamp_duty=Decimal("0.00"),
        total=Decimal("122.00"),
        purpose=None,
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
        vat_nature=None,
    )
    xml = _build_xml(invoice, issuer, client, [line], "202600003")
    assert validate_fatturapa(xml) == []
    # Both cedente and cessionario use Anagrafica/Nome+Cognome, not Denominazione.
    assert "<Nome>Angelo</Nome><Cognome>Leto</Cognome>" in xml
    assert "<Nome>Mario</Nome><Cognome>Rossi</Cognome>" in xml
    assert "<Denominazione>" not in xml


def test_recipient_pec_goes_in_pecdestinatario() -> None:
    issuer = IssuerProfile(
        country_code="IT",
        vat_number="13438810015",
        tax_code=None,
        legal_name="Acme Srl",
        tax_regime="RF01",
        address="Via Roma 1",
        postal_code="00100",
        city="Roma",
        province="RM",
        country="IT",
    )
    client = ClientProfile(
        legal_name="Mario Rossi",
        country_code="IT",
        vat_number=None,
        tax_code="RSSMRA80A01H501U",
        sdi_code=None,  # no SdI code: delivery is by PEC
        pec="mario@pec.example.it",
        address="Via Milano 2",
        postal_code="20100",
        city="Milano",
        province="MI",
        country="IT",
    )
    invoice = Invoice(
        document_type=DocumentType.TD01,
        currency="EUR",
        issued_at=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
        series="A",
        number=4,
        taxable=Decimal("100.00"),
        vat=Decimal("22.00"),
        stamp_duty=Decimal("0.00"),
        total=Decimal("122.00"),
        purpose=None,
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
        vat_nature=None,
    )
    xml = _build_xml(invoice, issuer, client, [line], "202600004")
    assert validate_fatturapa(xml) == []
    assert "<CodiceDestinatario>0000000</CodiceDestinatario>" in xml
    # The recipient PEC is the routing address (PECDestinatario), not the
    # transmitter's ContattiTrasmittente/Email.
    assert "<PECDestinatario>mario@pec.example.it</PECDestinatario>" in xml
    assert "<ContattiTrasmittente>" not in xml
