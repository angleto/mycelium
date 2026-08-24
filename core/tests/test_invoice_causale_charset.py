"""FatturaPA text charset normalisation and the additive forfettario causale.

Hermetic (no DB, no network): builds the XML through the same ``_build_xml``
the service uses and validates it against the vendored official schema, like
``test_invoice_xsd``.

Two behaviours are pinned here, and one of them is pinned mostly as a
non-regression guard: the operator transmits real forfettario documents through
this path, so "an invoice whose purpose already IS the dicitura still emits
exactly one Causale" matters more than any of the new cases.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from mycelium_core.models.client_profile import ClientProfile
from mycelium_core.models.invoice import DocumentType, Invoice, InvoiceLine, IssuerProfile
from mycelium_core.services.invoice_format import (
    FORFETTARIO_CAUSALE,
    _build_xml,
    fatturapa_text,
    is_forfettario_causale,
)
from mycelium_core.services.invoice_xsd import validate_fatturapa

#: The exact line description Stripe generates for a EUR subscription item.
#: Written with escapes because two of its characters ARE the subject under
#: test: U+00D7 MULTIPLICATION SIGN, which is inside Latin-1 and must survive
#: untouched, and U+20AC EURO SIGN, which is outside it and must become "EUR".
STRIPE_LINE = "1 \u00d7 Starter (at \u20ac50.00 / month)"
STRIPE_LINE_EMITTED = "1 \u00d7 Starter (at EUR50.00 / month)"


def _issuer(regime: str = "RF01") -> IssuerProfile:
    return IssuerProfile(
        country_code="IT",
        vat_number="01234567890",
        tax_code=None,
        legal_name="Acme Srl",
        tax_regime=regime,
        address="Via Roma 1",
        postal_code="00100",
        city="Roma",
        province="RM",
        country="IT",
    )


def _client(legal_name: str = "Client SpA") -> ClientProfile:
    return ClientProfile(
        legal_name=legal_name,
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


def _xml(
    *,
    regime: str = "RF01",
    purpose: str | None = None,
    notes: str | None = None,
    description: str = "consulting",
    client_name: str = "Client SpA",
) -> str:
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
        purpose=purpose,
        notes=notes,
        payment_iban=None,
        payment_due_date=None,
    )
    line = InvoiceLine(
        line_no=1,
        description=description,
        quantity=Decimal(2),
        unit_price=Decimal("100.00"),
        vat_rate=Decimal(22),
        vat_nature=None,
    )
    return _build_xml(invoice, _issuer(regime), _client(client_name), [line], "202600001")


def _causali(xml: str) -> list[str]:
    """The Causale texts in document order, read off the emitted string rather
    than off an XML parse: the point is what leaves the process."""
    out = []
    rest = xml
    while "<Causale>" in rest:
        _, _, rest = rest.partition("<Causale>")
        text, _, rest = rest.partition("</Causale>")
        out.append(text)
    return out


# --- fatturapa_text: the identity invariant comes first ----------------------


def test_normaliser_is_the_identity_on_text_the_facet_already_admits() -> None:
    """The load-bearing invariant: this function sits on the emission path of
    every document the system produces, so it must not move a byte of anything
    that validates today. Latin-1 covers the accented Italian range and the
    punctuation an invoice actually uses."""
    for value in (
        "consulting",
        "Perche' e' cosi: a e i o u",
        "Perché è così, però: à è ì ò ù",
        "Societa' Agricola S.r.l. - Via Gia' Bergamo, 12/A",
        "N. 1 x articolo (IVA 22%) @ 100.00 EUR",
        "±°§¿¡ÆØåæø ÀÈÌÒÙ ÿ",
    ):
        assert fatturapa_text(value) == value


def test_euro_sign_becomes_eur_and_the_multiplication_sign_survives() -> None:
    """The real Stripe line description. U+20AC (Currency Symbols) is outside
    the facet; U+00D7 (Latin-1 Supplement) is inside it and must be left alone,
    because transliterating it too would be a change with no cause."""
    assert fatturapa_text(STRIPE_LINE) == STRIPE_LINE_EMITTED


def test_line_breaks_become_spaces_rather_than_disappearing() -> None:
    """xs:normalizedString carries whiteSpace="replace", so SdI already turns a
    newline into a space before applying the pattern facet: a two-line note
    reaches the recipient as two words separated by a space, and validates.
    Dropping the character instead would weld the words together, which is a
    regression on text that works today."""
    assert fatturapa_text("riga uno\nriga due") == "riga uno riga due"
    assert fatturapa_text("a\tb\r\nc") == "a b  c"


def test_unmapped_characters_decompose_or_drop_rather_than_failing_the_document() -> None:
    assert fatturapa_text("Škoda s.r.o.") == "Skoda s.r.o."
    assert fatturapa_text("naïve ﬁne") == "naïve fine"  # ï is Latin-1 and stays
    # Nothing in the facet's range survives, so the element goes out empty and
    # the XSD gate refuses the document. That is the intended outcome: a name
    # with no Latin-1 rendering is a data problem no substitution can repair.
    assert fatturapa_text("株式会社") == ""


def test_normalisation_is_idempotent() -> None:
    once = fatturapa_text(f"{STRIPE_LINE}\nseconda riga")
    assert fatturapa_text(once) == once


# --- the additive forfettario causale ----------------------------------------


def test_forfettario_invoice_carrying_the_dicitura_emits_exactly_one_causale() -> None:
    """NON-REGRESSION. This is the shape of every forfettario document already
    transmitted: create_draft put the dicitura in ``purpose``, and the emitted
    XML must stay byte-identical to what it was before the causale became
    additive."""
    xml = _xml(regime="RF19", purpose=FORFETTARIO_CAUSALE)
    assert _causali(xml) == [FORFETTARIO_CAUSALE]
    assert validate_fatturapa(xml) == []


def test_forfettario_invoice_with_its_own_causale_keeps_the_statutory_one_too() -> None:
    """The fix. Causale is maxOccurs="unbounded", so an operator-written causale
    no longer displaces the L.190/2014 dicitura from the document."""
    xml = _xml(regime="RF19", purpose="Acconto contratto 2026")
    assert _causali(xml) == ["Acconto contratto 2026", FORFETTARIO_CAUSALE]
    assert validate_fatturapa(xml) == []


def test_the_dicitura_is_not_duplicated_by_stray_whitespace_or_typography() -> None:
    """The serializer and the PDF share one predicate for "is this the
    dicitura", and it compares normalised-and-stripped text: a value pasted
    with a trailing newline, or with a typographic apostrophe, is the same
    dicitura and must not be emitted twice."""
    typographic = FORFETTARIO_CAUSALE.replace("'", "\u2019")  # typographic apostrophe
    assert is_forfettario_causale(f"  {FORFETTARIO_CAUSALE}\n")
    assert is_forfettario_causale(typographic)
    assert _causali(_xml(regime="RF19", purpose=f"{FORFETTARIO_CAUSALE} ")) == [
        f"{FORFETTARIO_CAUSALE} "
    ]
    assert _causali(_xml(regime="RF19", purpose=typographic)) == [FORFETTARIO_CAUSALE]


def test_an_ordinary_regime_never_acquires_the_forfettario_dicitura() -> None:
    assert _causali(_xml(regime="RF01", purpose="Acconto contratto 2026")) == [
        "Acconto contratto 2026"
    ]
    assert _causali(_xml(regime="RF01")) == []


def test_notes_are_normalised_before_being_chunked_not_after() -> None:
    """A substitution lengthens the text, so slicing first would hand the
    String200LatinType facet a 202-character chunk. 200 euro signs become 600
    characters, hence three full chunks."""
    xml = _xml(notes="€" * 200)
    assert _causali(xml) == ["EUR" * 66 + "EU", "R" + "EUR" * 66 + "E", "UR" + "EUR" * 66]
    assert all(len(c) <= 200 for c in _causali(xml))
    assert validate_fatturapa(xml) == []


# --- end to end: the document that started this ------------------------------


def test_the_real_stripe_subscription_line_now_validates() -> None:
    """Before the normaliser this exact description, which Stripe generates by
    itself for every EUR subscription line, failed String1000LatinType and
    blocked the whole document at the gate."""
    xml = _xml(description=STRIPE_LINE)
    assert f"<Descrizione>{STRIPE_LINE_EMITTED}</Descrizione>" in xml
    assert validate_fatturapa(xml) == []


def test_a_foreign_counterpart_name_is_transliterated_rather_than_blocking() -> None:
    """Consequence worth pinning because it widens what the system is willing
    to file: the tracciato physically cannot carry "Š", so the alternative to
    transliterating is refusing to invoice a Czech company at all. The courtesy
    PDF still shows the real spelling (it is not bound by the tracciato)."""
    xml = _xml(client_name="Škoda Auto a.s.")
    assert "<Denominazione>Skoda Auto a.s.</Denominazione>" in xml
    assert validate_fatturapa(xml) == []
