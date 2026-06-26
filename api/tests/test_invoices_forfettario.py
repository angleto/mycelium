"""F7 forfettario (regime RF19) correctness, end-to-end (DB-backed).

Mirrors the two real reference invoices the product owner provided
(IT13438810015_UQ6IZ.xml pre-send / _X3RtZ.xml signed): a forfettario
issuer emits 0%-IVA + Natura N2.2 lines, the mandatory L.190/2014
purpose, virtual stamp duty (DatiBollo / BolloVirtuale SI) of EUR 2.00
when the taxable reaches the legal threshold, and
ImportoTotaleDocumento = taxable + stamp_duty. Also covers IBAN precedence
(invoice > client > issuer), the live XML/PDF/JSON previews of a draft
(no transmit), and a non-forfettario (RF01) regression that ordinary
behaviour is untouched.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app

_FORFETTARIO_CAUSALE = (
    "Operazione effettuata in regime forfettario ai sensi dell'articolo 1, "
    "commi da 54 a 89, della Legge n. 190/2014 e successive modificazioni"
)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _owner(c: AsyncClient) -> dict[str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "testp4ssw0rd", "workspace_name": "F"},
        )
    ).json()
    # Issuer-profile / client writes need the effective role admin;
    # X-Workspace-Role is clamped to the membership (owner here).
    return {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
        "X-Workspace-Role": "owner",
    }


async def test_forfettario_draft_lines_causale_bollo_and_xml_preview() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)

        # Forfettario issuer (RF19) with a fallback IBAN.
        prof = (
            await c.post(
                "/issuer-profiles",
                headers=h,
                json={
                    "label": "Ditta individuale",
                    "legal_name": "Angelo Leto",
                    "tax_code": "LTENGL79M31I356X",
                    "vat_number": "13438810015",
                    "tax_regime": "RF19",
                    "address": "Via Sandro Botticelli 77",
                    "postal_code": "10154",
                    "city": "Torino",
                    "province": "TO",
                    "default_iban": "IT92O0301503200000003396368",
                },
            )
        ).json()
        assert prof["tax_regime"] == "RF19"
        assert prof["default_iban"] == "IT92O0301503200000003396368"

        client = (
            await c.post(
                "/clients",
                headers=h,
                json={
                    "name": "Cylock",
                    "legal_name": "CYLOCK S.R.L.",
                    "country_code": "IT",
                    "vat_number": "16639311006",
                    "tax_code": "16639311006",
                    "address": "Via Fonte Buono 19/B",
                    "postal_code": "00142",
                    "city": "Roma",
                    "province": "RM",
                    "sdi_code": "KRRH6B9",
                },
            )
        ).json()

        # No purpose passed -> defaulted to the L.190/2014 string.
        inv = (
            await c.post(
                "/invoices",
                headers=h,
                # Pin series "A": this test asserts on the Numero (A1), it is not
                # exercising the per-client sezionale default.
                json={"client_tag_id": client["id"], "year": 2026, "series": "A"},
            )
        ).json()
        assert inv["purpose"] == _FORFETTARIO_CAUSALE
        assert inv["issuer_profile_id"] == prof["id"]
        # IBAN precedence: only issuer.default_iban set -> invoice gets it.
        assert inv["payment_iban"] == "IT92O0301503200000003396368"

        # Line with NO vat_rate / vat_nature passed -> forfettario defaults.
        ln = (
            await c.post(
                f"/invoices/{inv['id']}/lines",
                headers=h,
                json={
                    "description": "Attività di consulenza Tecnologica Aprile 2026",
                    "unit_price": "3731.00",
                    "quantity": "1",
                },
            )
        ).json()
        assert ln["vat_rate"] == "0.00"
        assert ln["vat_nature"] == "N2.2"

        # Totals: taxable 3731, vat 0, stamp_duty 2.00 (>= 77.47), total 3733.
        got = (await c.get(f"/invoices/{inv['id']}", headers=h)).json()
        assert got["taxable"] == "3731.00"
        assert got["vat"] == "0.00"
        assert got["stamp_duty"] == "2.00"
        assert got["total"] == "3733.00"  # taxable + stamp_duty

        # XML preview BEFORE transmit (never 404 for a valid draft).
        xml = (await c.get(f"/invoices/{inv['id']}/xml", headers=h)).json()["xml"]
        assert "<RegimeFiscale>RF19</RegimeFiscale>" in xml
        assert "<DatiBollo><BolloVirtuale>SI</BolloVirtuale>" in xml
        assert "<ImportoBollo>2.00</ImportoBollo></DatiBollo>" in xml
        assert "<ImportoTotaleDocumento>3733.00</ImportoTotaleDocumento>" in xml
        # DatiRiepilogo carries Natura after AliquotaIVA, before Imponibile.
        assert (
            "<DatiRiepilogo><AliquotaIVA>0.00</AliquotaIVA><Natura>N2.2</Natura>"
            "<ImponibileImporto>3731.00</ImponibileImporto>"
            "<Imposta>0.00</Imposta>" in xml
        )
        assert "<RiferimentoNormativo>" in xml  # forfettario default (#1)
        assert _FORFETTARIO_CAUSALE in xml
        # ANTEPRIMA progressivo + would-be number, no allocation.
        assert "<ProgressivoInvio>ANTEPRIMA</ProgressivoInvio>" in xml
        assert "<Numero>A-1</Numero>" in xml
        still = (await c.get(f"/invoices/{inv['id']}", headers=h)).json()
        assert still["number"] is None  # preview did not allocate

        # PDF preview: 200 application/pdf, %PDF magic.
        pdf = await c.get(f"/invoices/{inv['id']}/pdf", headers=h)
        assert pdf.status_code == 200
        assert pdf.headers["content-type"] == "application/pdf"
        assert pdf.content[:4] == b"%PDF"

        # JSON preview exposes the SdI address + effective IBAN + regime.
        prev = (await c.get(f"/invoices/{inv['id']}/preview", headers=h)).json()
        assert prev["is_forfettario"] is True
        assert prev["client"]["sdi_code"] == "KRRH6B9"
        assert prev["client"]["pec"] is None
        assert prev["effective_iban"] == "IT92O0301503200000003396368"
        assert prev["iban_source"] == "issuer"
        assert prev["totals"]["stamp_duty"] == "2.00"
        assert prev["totals"]["total"] == "3733.00"
        assert prev["number"] == "A-1"

        # Transmit freezes the same conformant document.
        tx = await c.post(f"/invoices/{inv['id']}/transmit", headers=h, json={})
        assert tx.status_code == 200
        body = tx.json()
        assert body["state"] == "transmitted"
        assert body["stamp_duty"] == "2.00"
        assert body["total"] == "3733.00"
        sent = (await c.get(f"/invoices/{inv['id']}/xml", headers=h)).json()["xml"]
        assert "<Numero>A-1</Numero>" in sent
        assert "ANTEPRIMA" not in sent  # real progressivo now


async def test_forfettario_bollo_below_threshold_is_zero() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        prof = (
            await c.post(
                "/issuer-profiles",
                headers=h,
                json={
                    "label": "DI",
                    "legal_name": "Angelo Leto",
                    "tax_code": "LTENGL79M31I356X",
                    "vat_number": "13438810015",
                    "tax_regime": "RF19",
                    "address": "Via X 1",
                    "postal_code": "10100",
                    "city": "Torino",
                },
            )
        ).json()
        assert prof["tax_regime"] == "RF19"
        client = (
            await c.post(
                "/clients",
                headers=h,
                json={
                    "name": "C",
                    "legal_name": "C SRL",
                    "country_code": "IT",
                    "vat_number": "16639311006",
                    "sdi_code": "KRRH6B9",
                    "address": "Via Cliente 5",
                    "postal_code": "20100",
                    "city": "Milano",
                },
            )
        ).json()
        inv = (
            await c.post("/invoices", headers=h, json={"client_tag_id": client["id"], "year": 2026})
        ).json()
        # 50.00 < 77.47 threshold -> no stamp_duty.
        await c.post(
            f"/invoices/{inv['id']}/lines",
            headers=h,
            json={"description": "small", "unit_price": "50.00", "quantity": "1"},
        )
        got = (await c.get(f"/invoices/{inv['id']}", headers=h)).json()
        assert got["taxable"] == "50.00"
        assert got["stamp_duty"] == "0.00"
        assert got["total"] == "50.00"  # taxable + stamp_duty(0)
        xml = (await c.get(f"/invoices/{inv['id']}/xml", headers=h)).json()["xml"]
        assert "<DatiBollo>" not in xml
        assert "<Natura>N2.2</Natura>" in xml  # still 0% + Natura
        assert "<RiferimentoNormativo>" in xml  # forfettario default (#1)


async def test_iban_precedence_invoice_over_client_over_issuer() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        prof = (
            await c.post(
                "/issuer-profiles",
                headers=h,
                json={
                    "label": "DI",
                    "legal_name": "Angelo Leto",
                    "tax_code": "LTENGL79M31I356X",
                    "vat_number": "13438810015",
                    "tax_regime": "RF19",
                    "address": "Via X 1",
                    "postal_code": "10100",
                    "city": "Torino",
                    "default_iban": "IT00ISSUER000000000000000000000",
                },
            )
        ).json()
        assert prof["default_iban"] == "IT00ISSUER000000000000000000000"

        # 1) Only issuer.default_iban -> invoice gets the issuer IBAN.
        c1 = (
            await c.post(
                "/clients",
                headers=h,
                json={
                    "name": "NoIban",
                    "legal_name": "No Iban SRL",
                    "country_code": "IT",
                    "vat_number": "16639311006",
                    "sdi_code": "KRRH6B9",
                },
            )
        ).json()
        i1 = (
            await c.post("/invoices", headers=h, json={"client_tag_id": c1["id"], "year": 2026})
        ).json()
        assert i1["payment_iban"] == "IT00ISSUER000000000000000000000"
        prev1 = (await c.get(f"/invoices/{i1['id']}/preview", headers=h)).json()
        assert prev1["iban_source"] == "issuer"

        # 2) client.payment_iban overrides the issuer default.
        c2 = (
            await c.post(
                "/clients",
                headers=h,
                json={
                    "name": "CliIban",
                    "legal_name": "Cli Iban SRL",
                    "country_code": "IT",
                    "vat_number": "16639311006",
                    "sdi_code": "KRRH6B9",
                    "payment_iban": "IT11CLIENT000000000000000000000",
                },
            )
        ).json()
        # POST /clients returns a TagOut; the invoicing card (with
        # payment_iban) is on ClientOut, surfaced by GET /clients.
        listed = (await c.get("/clients", headers=h)).json()
        c2_full = next(x for x in listed if x["id"] == c2["id"])
        assert c2_full["payment_iban"] == "IT11CLIENT000000000000000000000"
        i2 = (
            await c.post("/invoices", headers=h, json={"client_tag_id": c2["id"], "year": 2026})
        ).json()
        assert i2["payment_iban"] == "IT11CLIENT000000000000000000000"
        prev2 = (await c.get(f"/invoices/{i2['id']}/preview", headers=h)).json()
        assert prev2["iban_source"] == "client"

        # 3) An explicit invoice IBAN wins over both.
        patched = await c.patch(
            f"/invoices/{i2['id']}",
            headers=h,
            json={"payment_iban": "IT22INVOICE00000000000000000000"},
        )
        assert patched.status_code == 200
        assert patched.json()["payment_iban"] == "IT22INVOICE00000000000000000000"
        prev3 = (await c.get(f"/invoices/{i2['id']}/preview", headers=h)).json()
        assert prev3["effective_iban"] == "IT22INVOICE00000000000000000000"
        assert prev3["iban_source"] == "invoice"


async def test_ordinary_regime_rf01_is_untouched() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        # Default regime RF01 (ordinary): no forfettario behaviour.
        prof = (
            await c.post(
                "/issuer-profiles",
                headers=h,
                json={
                    "label": "Srl",
                    "legal_name": "Acme Srl",
                    "vat_number": "01234567890",
                    "address": "Via Roma 1",
                    "postal_code": "00100",
                    "city": "Roma",
                },
            )
        ).json()
        assert prof["tax_regime"] == "RF01"
        client = (
            await c.post(
                "/clients",
                headers=h,
                json={
                    "name": "Cli",
                    "legal_name": "Cli SpA",
                    "country_code": "IT",
                    "vat_number": "09876543210",
                    "sdi_code": "ABCDEFG",
                    "address": "Via Cliente 5",
                    "postal_code": "20100",
                    "city": "Milano",
                },
            )
        ).json()
        inv = (
            await c.post("/invoices", headers=h, json={"client_tag_id": client["id"], "year": 2026})
        ).json()
        # No forfettario purpose defaulted for an ordinary issuer.
        assert inv["purpose"] is None
        # No vat_rate passed -> ordinary default 22%, no Natura.
        ln = (
            await c.post(
                f"/invoices/{inv['id']}/lines",
                headers=h,
                json={"description": "consulting", "unit_price": "100.00", "quantity": "2"},
            )
        ).json()
        assert ln["vat_rate"] == "22.00"
        assert ln["vat_nature"] is None
        got = (await c.get(f"/invoices/{inv['id']}", headers=h)).json()
        assert got["taxable"] == "200.00"
        assert got["vat"] == "44.00"
        assert got["stamp_duty"] == "0.00"
        assert got["total"] == "244.00"  # 200 + 22%, no stamp_duty
        xml = (await c.get(f"/invoices/{inv['id']}/xml", headers=h)).json()["xml"]
        assert "<DatiBollo>" not in xml
        assert "<Natura>" not in xml
        assert "<ImportoTotaleDocumento>244.00</ImportoTotaleDocumento>" in xml
        assert "RF01" in xml
