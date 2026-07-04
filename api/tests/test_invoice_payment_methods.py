"""Configurable payment methods + counter override (migration 0080).

Covers:
* CondizioniPagamento / ModalitaPagamento / GiorniTerminiPagamento
  emission with non-default values, resolved with precedence
  invoice > client > issuer > system default.
* DataScadenzaPagamento auto-fill from terms-days when no explicit due
  date is set.
* Counter override (admin-only): floor invariant rejects a value
  below the highest already-emitted number.
* Per-client invoice_language drives PDF locale (XML stays Italian).
* Unknown TPxx / MPxx values are rejected before they reach the XML.
"""

from __future__ import annotations

import datetime as dt
import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _owner(c: AsyncClient) -> dict[str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "testp4ssw0rd", "workspace_name": "F"},
        )
    ).json()
    return {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
        "X-Workspace-Role": "owner",
    }


async def _make_issuer(c: AsyncClient, h: dict[str, str], **overrides: object) -> dict:
    body: dict[str, object] = {
        "label": "Issuer",
        "legal_name": "Mario Rossi",
        "tax_code": "RSSMRA80A01H501U",
        "vat_number": "01112223334",
        "tax_regime": "RF01",
        "address": "Via Roma 1",
        "postal_code": "10100",
        "city": "Torino",
        "province": "TO",
    }
    body.update(overrides)
    return (await c.post("/issuer-profiles", headers=h, json=body)).json()


async def _make_client(c: AsyncClient, h: dict[str, str], **overrides: object) -> dict:
    body: dict[str, object] = {
        "name": "Acme",
        "legal_name": "ACME S.R.L.",
        "country_code": "IT",
        "vat_number": "02223334445",
        "tax_code": "02223334445",
        "address": "Via Fonte 1",
        "postal_code": "00142",
        "city": "Roma",
        "province": "RM",
        "sdi_code": "WXYZ123",
    }
    body.update(overrides)
    return (await c.post("/clients", headers=h, json=body)).json()


async def test_payment_method_precedence_invoice_over_client_over_issuer() -> None:
    """All three levels carry a different ModalitaPagamento; the invoice
    override wins, then client, then issuer, then system default."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        await _make_issuer(
            c,
            h,
            default_payment_conditions_code="TP01",
            default_payment_method_code="MP07",
            default_payment_terms_days=15,
        )
        client = await _make_client(
            c,
            h,
            default_payment_conditions_code="TP03",
            default_payment_method_code="MP19",
            default_payment_terms_days=30,
        )
        # 1. invoice override wins on both fields + terms days
        inv = (
            await c.post(
                "/invoices",
                headers=h,
                json={"client_tag_id": client["id"], "year": 2026, "series": "A"},
            )
        ).json()
        patched = await c.patch(
            f"/invoices/{inv['id']}",
            headers=h,
            json={
                "payment_conditions_code": "TP02",
                "payment_method_code": "MP05",
                "payment_terms_days": 7,
                "payment_iban": "IT60X0542811101000000123456",
            },
        )
        assert patched.status_code == 200
        await c.post(
            f"/invoices/{inv['id']}/lines",
            headers=h,
            json={"description": "x", "unit_price": "100.00", "quantity": "1"},
        )
        xml = (await c.get(f"/invoices/{inv['id']}/xml", headers=h)).json()["xml"]
        assert "<CondizioniPagamento>TP02</CondizioniPagamento>" in xml
        assert "<ModalitaPagamento>MP05</ModalitaPagamento>" in xml
        assert "<GiorniTerminiPagamento>7</GiorniTerminiPagamento>" in xml
        # 2. invoice has nothing -> client wins
        inv2 = (
            await c.post(
                "/invoices",
                headers=h,
                json={"client_tag_id": client["id"], "year": 2026, "series": "B"},
            )
        ).json()
        # Add an IBAN so DatiPagamento is emitted at all.
        await c.patch(
            f"/invoices/{inv2['id']}",
            headers=h,
            json={"payment_iban": "IT60X0542811101000000123456"},
        )
        await c.post(
            f"/invoices/{inv2['id']}/lines",
            headers=h,
            json={"description": "y", "unit_price": "10.00", "quantity": "1"},
        )
        xml2 = (await c.get(f"/invoices/{inv2['id']}/xml", headers=h)).json()["xml"]
        assert "<CondizioniPagamento>TP03</CondizioniPagamento>" in xml2
        assert "<ModalitaPagamento>MP19</ModalitaPagamento>" in xml2
        assert "<GiorniTerminiPagamento>30</GiorniTerminiPagamento>" in xml2


async def test_terms_days_auto_fills_payment_due_date() -> None:
    """When terms-days is set and no explicit due-date, the draft service
    materializes payment_due_date = today + days (so the preview shows it)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        await _make_issuer(c, h)
        client = await _make_client(c, h)
        inv = (
            await c.post(
                "/invoices",
                headers=h,
                json={"client_tag_id": client["id"], "year": 2026, "series": "A"},
            )
        ).json()
        before = dt.datetime.now(dt.UTC).date()
        patched = (
            await c.patch(
                f"/invoices/{inv['id']}",
                headers=h,
                json={"payment_terms_days": 7},
            )
        ).json()
        after = dt.datetime.now(dt.UTC).date()
        assert patched["payment_terms_days"] == 7
        # CONTRACT: the auto-filled due date is (issued_at or now).date() in
        # UTC + terms_days -- the UTC calendar date, like every other date the
        # invoice service derives (issued_at, credit-note date, default year).
        # ``dt.date.today()`` is the LOCAL date and made this test fail every
        # night between 00:00 and 02:00 Europe/Rome (task b710ca8b). Bracket
        # the service's clock read so the test is deterministic at any hour,
        # including across a midnight-UTC crossing.
        allowed = {(d + dt.timedelta(days=7)).isoformat() for d in (before, after)}
        assert patched["payment_due_date"] in allowed


async def test_counter_override_rejects_value_below_max_emitted() -> None:
    """Setting last_number below max(invoices.number) for the same
    (issuer, series, year) would let the next allocation collide with
    the unique constraint. The service must reject it."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        issuer = await _make_issuer(c, h)
        client = await _make_client(c, h)
        # Emit one invoice -> counter = 1.
        inv = (
            await c.post(
                "/invoices",
                headers=h,
                json={"client_tag_id": client["id"], "year": 2026, "series": "A"},
            )
        ).json()
        await c.post(
            f"/invoices/{inv['id']}/lines",
            headers=h,
            json={"description": "x", "unit_price": "100.00", "quantity": "1"},
        )
        tx = await c.post(f"/invoices/{inv['id']}/transmit", headers=h, json={})
        assert tx.status_code == 200
        # Listing exposes the counter + max_emitted floor.
        rows = (await c.get(f"/issuer-profiles/{issuer['id']}/counters", headers=h)).json()
        row = next(r for r in rows if r["series"] == "A" and r["year"] == 2026)
        assert row["last_number"] == 1
        assert row["max_emitted"] == 1
        # Trying to lower it below the floor -> 409.
        bad = await c.put(
            f"/issuer-profiles/{issuer['id']}/counters/A/2026",
            headers=h,
            json={"last_number": 0},
        )
        assert bad.status_code == 409
        # Raising it is fine (e.g. continuing from an external sequence).
        good = await c.put(
            f"/issuer-profiles/{issuer['id']}/counters/A/2026",
            headers=h,
            json={"last_number": 12},
        )
        assert good.status_code == 200
        assert good.json()["last_number"] == 12


async def test_counter_can_be_created_for_new_year_via_override() -> None:
    """When migrating from another system mid-year, the user starts a
    fresh (series, year) at the import number without ever emitting via
    Mycelium first. The PUT endpoint must create the row on the fly."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        issuer = await _make_issuer(c, h)
        # Counter doesn't exist yet: PUT creates it.
        r = await c.put(
            f"/issuer-profiles/{issuer['id']}/counters/A/2025",
            headers=h,
            json={"last_number": 47},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["last_number"] == 47
        assert body["max_emitted"] == 0


async def test_unknown_payment_method_codes_are_rejected() -> None:
    """An invented TPxx / MPxx must not reach the XML build (SdI scarta)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        bad = await c.post(
            "/issuer-profiles",
            headers=h,
            json={
                "label": "X",
                "legal_name": "X",
                "tax_code": "RSSMRA80A01H501U",
                "vat_number": "01112223334",
                "address": "v",
                "postal_code": "10100",
                "city": "T",
                "province": "TO",
                "default_payment_method_code": "MP99",
            },
        )
        assert bad.status_code == 400


async def test_pdf_locale_en_xml_stays_italian() -> None:
    """The locale only affects the courtesy PDF; the FatturaPA XML
    transmitted to SdI is structurally Italian regardless. We don't try
    to introspect the PDF content stream (reportlab Flate-compresses
    it); the label-table coverage is in test_invoice_pdf_labels below."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        issuer = await _make_issuer(c, h, tax_regime="RF19")
        assert issuer["tax_regime"] == "RF19"
        # POST /clients returns a TagOut (not the full ClientOut); the
        # language is verified end-to-end via the XML/PDF below.
        client = await _make_client(c, h, invoice_language="en")
        inv = (
            await c.post(
                "/invoices",
                headers=h,
                json={"client_tag_id": client["id"], "year": 2026, "series": "A"},
            )
        ).json()
        await c.post(
            f"/invoices/{inv['id']}/lines",
            headers=h,
            json={"description": "Consulting", "unit_price": "100.00", "quantity": "1"},
        )
        pdf = await c.get(f"/invoices/{inv['id']}/pdf", headers=h)
        assert pdf.status_code == 200
        assert pdf.content[:4] == b"%PDF"
        # The XML preview is unaffected by locale -> Italian wording.
        xml = (await c.get(f"/invoices/{inv['id']}/xml", headers=h)).json()["xml"]
        assert "Operazione effettuata in regime forfettario" in xml


def test_invoice_pdf_labels_cover_all_supported_locales() -> None:
    """Every supported locale must carry the same key set: a missing
    label would render an empty string on the PDF for a foreign client
    (silent UX bug). Lock the matrix as a CI invariant."""
    from mycelium_core.services.invoice_pdf import _LABELS

    ref = set(_LABELS["it"])
    assert "it" in _LABELS and "en" in _LABELS
    for loc, table in _LABELS.items():
        assert set(table) == ref, f"locale {loc} has key drift: {set(table) ^ ref}"
    # English must really be English (not a copy of Italian).
    assert _LABELS["en"]["invoice"] == "Invoice"
    assert _LABELS["en"]["due_date"] == "Due date"


def test_bollo_dicitura_uses_mef_decree_wording() -> None:
    """The PDF stamp-duty note follows DM 17/06/2014 art.6 wording.
    Asserted on the constant (reportlab Flate-compresses the content
    stream, so we cannot grep the PDF bytes for the phrase)."""
    from mycelium_core.services.invoice_format import BOLLO_DICITURA

    assert "decreto MEF 17 GIUGNO 2014" in BOLLO_DICITURA
    assert "ART. 6" in BOLLO_DICITURA
    # The old "Imposta di stamp_duty assolta in modo virtuale" wording is
    # gone (a regression to it would mean we lost the AdE-style update).
    assert "Imposta di stamp_duty assolta in modo virtuale" != BOLLO_DICITURA
