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
    _fatturapa_email,
    _fatturapa_phone,
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
        vat_number="01112223334",
        tax_code="RSSMRA80A01H501U",
        legal_name="Mario Rossi",
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
    assert _bare_id_codice("IT01112223334", "IT") == "01112223334"
    assert _bare_id_codice("01112223334", "IT") == "01112223334"
    assert _bare_id_codice("RSSMRA80A01H501U", "IT") == "RSSMRA80A01H501U"
    assert _bare_id_codice("IT01112223334", "it") == "01112223334"


def test_country_prefixed_piva_emits_bare_idcodice_and_validates() -> None:
    # Regression: an issuer/client whose stored VAT carries the IdPaese prefix
    # (e.g. "IT01112223334") must still emit an 11-digit IdCodice -- the
    # backend never assembles country+number, so SdI accepts the cedente.
    issuer = IssuerProfile(
        country_code="IT",
        vat_number="IT01112223334",
        tax_code="RSSMRA80A01H501U",
        legal_name="Mario Rossi",
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
    assert "<IdCodice>01112223334</IdCodice>" in xml
    assert "<IdCodice>09876543210</IdCodice>" in xml
    assert "IT01112223334" not in xml
    # IdTrasmittente/IdCodice is a CODICE FISCALE for SdI: a physical-person
    # channel holder must transmit under the 16-char CF, not the P.IVA (a P.IVA
    # there is scartata 00300). The cedente IdFiscaleIVA above still carries the
    # P.IVA (01112223334).
    assert (
        "<IdTrasmittente><IdPaese>IT</IdPaese>"
        "<IdCodice>RSSMRA80A01H501U</IdCodice></IdTrasmittente>" in xml
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
        vat_number="01112223334",
        tax_code="RSSMRA80A01H501U",
        legal_name="Mario Rossi",
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
        vat_number="01112223334",
        tax_code="RSSMRA80A01H501U",
        legal_name="Mario Rossi",
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
        vat_number="01112223334",
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


def _xml_with_issuer_contacts(
    *,
    phone: str | None = None,
    fax: str | None = None,
    email: str | None = None,
    show_phone: bool | None = None,
    show_email: bool | None = None,
) -> str:
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
        phone=phone,
        fax=fax,
        email=email,
        show_phone=show_phone,
        show_email=show_email,
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


def test_fatturapa_phone_normalises_or_omits() -> None:
    # Human formats reduce to bare digits within 5-12 (the + and separators drop).
    assert _fatturapa_phone("+39 333 1234567") == "393331234567"  # 12 digits, fits
    assert _fatturapa_phone("06 1234567") == "061234567"
    assert _fatturapa_phone("333-12-345") == "33312345"
    # Out of range -> omitted (None): too many digits, or too few.
    assert _fatturapa_phone("+1 (555) 010-0199 9999") is None  # 15 digits
    assert _fatturapa_phone("123") is None  # < 5 digits
    assert _fatturapa_phone("") is None
    assert _fatturapa_phone(None) is None


def test_fatturapa_email_validates_or_omits() -> None:
    assert _fatturapa_email("info@acme.it") == "info@acme.it"
    assert _fatturapa_email("  info@acme.it  ") == "info@acme.it"
    assert _fatturapa_email("a@b.co") is None  # 6 chars < 7
    assert _fatturapa_email("posta@società.it") is None  # non-ASCII (IDN)
    assert _fatturapa_email(None) is None


def test_issuer_phone_with_prefix_normalises_and_validates() -> None:
    # Regression (prod invoice afef29c3-...): a cedente phone stored as
    # "+39 333 1234567" (13-char "+39"+number) scarto'd the WHOLE invoice on the
    # OPTIONAL Telefono (XSD pattern {5,12}); preview never ran XSD so it looked
    # fine, transmit 400'd. It must now emit bare digits and validate.
    xml = _xml_with_issuer_contacts(phone="+39 333 1234567")
    assert validate_fatturapa(xml) == []
    assert "<Telefono>393331234567</Telefono>" in xml


def test_issuer_phone_too_long_is_omitted_not_scarto() -> None:
    # A genuinely out-of-range number can't be made to fit -> the optional
    # element is dropped (never emitted malformed) and the document still
    # validates. No other contact is set, so the whole Contatti block is gone.
    xml = _xml_with_issuer_contacts(phone="+1 (555) 010-0199 9999")  # 15 digits
    assert validate_fatturapa(xml) == []
    assert "<Telefono>" not in xml
    assert "<Contatti>" not in xml


def test_issuer_nonascii_email_is_omitted_phone_kept() -> None:
    # A non-conformant email is dropped but a valid phone still emits: one bad
    # optional field never blocks the document or the other contacts.
    xml = _xml_with_issuer_contacts(phone="06 1234567", email="posta@società.it")
    assert validate_fatturapa(xml) == []
    assert "<Telefono>061234567</Telefono>" in xml
    assert "<Email>" not in xml


def test_issuer_contact_visibility_flags_gate_emission() -> None:
    # show_phone/show_email explicitly False hide an otherwise-valid contact
    # from the XML Contatti; the document stays valid either way.
    xml = _xml_with_issuer_contacts(
        phone="06 1234567", email="info@acme.it", show_phone=False, show_email=True
    )
    assert validate_fatturapa(xml) == []
    assert "<Telefono>" not in xml  # hidden
    assert "<Email>info@acme.it</Email>" in xml  # shown
    # Both hidden -> the whole Contatti block disappears.
    xml2 = _xml_with_issuer_contacts(
        phone="06 1234567", email="info@acme.it", show_phone=False, show_email=False
    )
    assert validate_fatturapa(xml2) == []
    assert "<Contatti>" not in xml2
    # Unset flags (None, e.g. a transient/in-memory profile) still show.
    xml3 = _xml_with_issuer_contacts(phone="06 1234567")
    assert "<Telefono>061234567</Telefono>" in xml3


def _xml_with_civici(*, issuer_civic: str | None = None, client_civic: str | None = None) -> str:
    issuer = IssuerProfile(
        country_code="IT",
        vat_number="01234567890",
        tax_code=None,
        legal_name="Acme Srl",
        tax_regime="RF01",
        address="Via Roma",
        civic_number=issuer_civic,
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
        address="Via Milano",
        civic_number=client_civic,
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


def test_numero_civico_emitted_for_both_parties() -> None:
    # When the dedicated field is set on cedente AND cessionario, NumeroCivico
    # is emitted in each Sede (XSD order: after Indirizzo, before CAP).
    xml = _xml_with_civici(issuer_civic="77", client_civic="12/A")
    assert validate_fatturapa(xml) == []
    assert xml.count("<NumeroCivico>") == 2
    assert "<Indirizzo>Via Roma</Indirizzo><NumeroCivico>77</NumeroCivico>" in xml
    assert "<Indirizzo>Via Milano</Indirizzo><NumeroCivico>12/A</NumeroCivico>" in xml


def test_numero_civico_omitted_when_absent() -> None:
    # No dedicated field -> no element (the civic number may live inline in
    # Indirizzo); the document still validates.
    xml = _xml_with_civici()
    assert validate_fatturapa(xml) == []
    assert "<NumeroCivico>" not in xml


def test_numero_civico_too_long_is_omitted() -> None:
    # NumeroCivico XSD maxLength is 8; an over-long value is dropped (optional)
    # rather than emitted malformed.
    xml = _xml_with_civici(issuer_civic="123456789")  # 9 chars
    assert validate_fatturapa(xml) == []
    assert "<NumeroCivico>" not in xml
