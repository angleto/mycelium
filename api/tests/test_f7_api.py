"""F7 API end-to-end (DB-backed): issuer profiles (multi + default +
per-invoice selection), invoice draft editing (invoice fields + lines
add/edit/delete), transmit (manual export -> out of AdE coverage),
immutability, XML download (notes -> Causale, IBAN -> DatiPagamento),
TD04 credit note, cross-org isolation."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_f7_api_flow() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "A"},
            )
        ).json()
        b = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "B"},
            )
        ).json()
        # Owner with full entitlement: issuer-profile writes need the
        # effective role admin, which is X-Workspace-Role clamped to
        # the membership (absent header => member, least privilege).
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }

        # First issuer profile is auto-default.
        p1 = await c.post(
            "/issuer-profiles",
            headers=h,
            json={
                "label": "Ditta individuale",
                "legal_name": "Acme Srl",
                "vat_number": "01234567890",
                "address": "Via Roma 1",
                "civic_number": "77",
                "postal_code": "00100",
                "city": "Roma",
            },
        )
        assert p1.status_code == 200
        prof1 = p1.json()
        assert prof1["legal_name"] == "Acme Srl" and prof1["is_default"]
        assert prof1["civic_number"] == "77"
        # PATCH the optional civic number (exercises the _PROFILE_FIELDS allowlist).
        ep = await c.patch(
            f"/issuer-profiles/{prof1['id']}", headers=h, json={"civic_number": "77/B"}
        )
        assert ep.status_code == 200 and ep.json()["civic_number"] == "77/B"

        # A second profile; explicitly promote it to default.
        p2 = await c.post(
            "/issuer-profiles",
            headers=h,
            json={
                "label": "SRL",
                "legal_name": "Acme Holding Srl",
                "vat_number": "01234567890",
                "address": "Via Milano 2",
                "postal_code": "20100",
                "city": "Milano",
                "is_default": True,
            },
        )
        prof2 = p2.json()
        listed = (await c.get("/issuer-profiles", headers=h)).json()
        assert len(listed) == 2
        defaults = {x["id"]: x["is_default"] for x in listed}
        assert defaults[prof2["id"]] and not defaults[prof1["id"]]

        client = (
            await c.post(
                "/clients",
                headers=h,
                json={
                    "name": "Client SpA",
                    "legal_name": "Client SpA",
                    "country_code": "IT",
                    "vat_number": "09876543210",
                    "sdi_code": "ABCDEFG",
                    "address": "Via Milano 2",
                    "civic_number": "12/A",
                    "postal_code": "20100",
                    "city": "Milano",
                },
            )
        ).json()
        # POST /clients returns the Tag; read the profile back via GET /clients
        # (ClientOut) to assert the civic_number round-trips through _client_out.
        clients_list = (await c.get("/clients", headers=h)).json()
        cprof = next(x for x in clients_list if x["id"] == client["id"])
        assert cprof["civic_number"] == "12/A"

        # No issuer given -> the default (prof2) is pre-selected. We
        # immediately delete that draft because we want this test to
        # exercise the prof1 issuer's identity in the XML below — and
        # a draft's billing identity (client + issuer) is FROZEN at
        # create_draft now (see _DRAFT_UPDATABLE): switching issuer
        # would silently re-key the (issuer, series, year) counter
        # under an existing draft, so the supported workflow is to
        # delete and recreate with the desired issuer.
        inv = (
            await c.post(
                "/invoices",
                headers=h,
                json={"client_tag_id": client["id"], "year": 2026},
            )
        ).json()
        assert inv["number"] is None  # not allocated until transmit
        assert inv["issuer_profile_id"] == prof2["id"]
        rm = await c.delete(f"/invoices/{inv['id']}", headers=h)
        assert rm.status_code == 204

        # Re-create pinned to prof1 from the start; fill the remaining
        # draft-only fields via PATCH (notes / IBAN / due — none of
        # these is in the immutable identity set).
        inv = (
            await c.post(
                "/invoices",
                headers=h,
                json={
                    "client_tag_id": client["id"],
                    "year": 2026,
                    "issuer_profile_id": prof1["id"],
                },
            )
        ).json()
        assert inv["issuer_profile_id"] == prof1["id"]
        patched = await c.patch(
            f"/invoices/{inv['id']}",
            headers=h,
            json={
                "notes": "Grazie per la collaborazione",
                "payment_iban": "IT60X0542811101000000123456",
                "payment_due_date": "2026-07-31",
            },
        )
        assert patched.status_code == 200
        pj = patched.json()
        assert pj["issuer_profile_id"] == prof1["id"]
        assert pj["payment_iban"] == "IT60X0542811101000000123456"

        ln = (
            await c.post(
                f"/invoices/{inv['id']}/lines",
                headers=h,
                json={"description": "consulting", "unit_price": "100.00", "quantity": "2"},
            )
        ).json()
        # Editable while draft: fix the line, add then drop a throwaway.
        up = await c.put(
            f"/invoices/{inv['id']}/lines/{ln['id']}",
            headers=h,
            json={"description": "consulting (rev)", "unit_price": "100.00", "quantity": "2"},
        )
        assert up.status_code == 200 and up.json()["description"] == "consulting (rev)"
        extra = (
            await c.post(
                f"/invoices/{inv['id']}/lines",
                headers=h,
                json={"description": "drop me", "unit_price": "5"},
            )
        ).json()
        rm = await c.delete(f"/invoices/{inv['id']}/lines/{extra['id']}", headers=h)
        assert rm.status_code == 204
        assert len((await c.get(f"/invoices/{inv['id']}/lines", headers=h)).json()) == 1

        tx = await c.post(f"/invoices/{inv['id']}/transmit", headers=h, json={})
        assert tx.status_code == 200
        body = tx.json()
        assert body["number"] == 1 and body["state"] == "transmitted"
        assert body["total"] == "244.00"  # 200 + 22%
        # SdI notification timeline + signed-XML download endpoint are wired:
        # the list is addressable (200) and a bogus notification id is a 404.
        notifs = await c.get(f"/invoices/{inv['id']}/notifications", headers=h)
        assert notifs.status_code == 200 and isinstance(notifs.json(), list)
        miss = await c.get(f"/invoices/{inv['id']}/notifications/{uuid.uuid4()}/xml", headers=h)
        assert miss.status_code == 404
        assert body["conservation_status"] == "out_of_coverage"

        xml = (await c.get(f"/invoices/{inv['id']}/xml", headers=h)).json()["xml"]
        assert "FatturaElettronica" in xml and "FPR12" in xml
        assert "Grazie per la collaborazione" in xml  # notes -> Causale
        assert "IT60X0542811101000000123456" in xml  # IBAN -> DatiPagamento
        assert "Via Roma 1" in xml  # prof1's address, the chosen issuer
        # NumeroCivico emitted for both parties (issuer patched to 77/B, client 12/A).
        assert "<NumeroCivico>77/B</NumeroCivico>" in xml
        assert "<NumeroCivico>12/A</NumeroCivico>" in xml

        # Immutable after emission.
        late = await c.post(
            f"/invoices/{inv['id']}/lines",
            headers=h,
            json={"description": "late", "unit_price": "1"},
        )
        assert late.status_code == 409
        late_patch = await c.patch(f"/invoices/{inv['id']}", headers=h, json={"notes": "too late"})
        assert late_patch.status_code == 409

        # A profile used by an invoice cannot be deleted.
        del_used = await c.delete(f"/issuer-profiles/{prof1['id']}", headers=h)
        assert del_used.status_code == 409

        cn = await c.post(
            "/invoices/credit-note",
            headers=h,
            json={"parent_invoice_id": inv["id"]},
        )
        assert cn.status_code == 200 and cn.json()["document_type"] == "TD04"

        paid = await c.post(f"/invoices/{inv['id']}/paid", headers=h)
        assert paid.status_code == 200 and paid.json()["payment_status"] == "paid"

        cross = await c.get(
            "/invoices",
            headers={"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": b["workspace_id"]},
        )
        assert cross.status_code == 403


async def test_preview_carries_issuer_contacts_with_visibility() -> None:
    # The /invoices/{id}/preview issuer party must surface the cedente
    # contacts (phone/email/pec) + their per-contact visibility toggles so
    # the SPA renders the issuer block exactly as the emitted document carries
    # it. show_pec=False here must round-trip as False (a hidden contact the
    # SPA omits), proving the flag is projected, not defaulted.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "A"},
            )
        ).json()
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }
        await c.post(
            "/issuer-profiles",
            headers=h,
            json={
                "label": "Ditta",
                "legal_name": "Acme Srl",
                "vat_number": "01234567890",
                "address": "Via Roma 1",
                "civic_number": "77",
                "postal_code": "00100",
                "city": "Roma",
                "province": "RM",
                "phone": "0000000000",
                "email": "info@example.test",
                "pec": "pec@example.test",
                "show_pec": False,
            },
        )
        client = (
            await c.post(
                "/clients",
                headers=h,
                json={
                    "name": "Client SpA",
                    "legal_name": "Client SpA",
                    "country_code": "IT",
                    "vat_number": "09876543210",
                    "sdi_code": "ABCDEFG",
                    "address": "Via Milano 2",
                    "civic_number": "19/B",
                    "postal_code": "20100",
                    "city": "Milano",
                    "province": "MI",
                },
            )
        ).json()
        inv = (
            await c.post("/invoices", headers=h, json={"client_tag_id": client["id"], "year": 2026})
        ).json()
        await c.post(
            f"/invoices/{inv['id']}/lines",
            headers=h,
            json={"description": "consulting", "unit_price": "100.00"},
        )
        pv = (await c.get(f"/invoices/{inv['id']}/preview", headers=h)).json()
        issuer = pv["issuer"]
        assert issuer["civic_number"] == "77"
        assert issuer["phone"] == "0000000000"
        assert issuer["email"] == "info@example.test"
        assert issuer["pec"] == "pec@example.test"
        # Defaults show; the explicitly-disabled toggle round-trips as False.
        assert issuer["show_phone"] is True
        assert issuer["show_email"] is True
        assert issuer["show_pec"] is False
        # Client civic number is projected too (cessionario address).
        assert pv["client"]["civic_number"] == "19/B"


async def test_persona_fisica_issuer_optional_legal_name() -> None:
    # FatturaPA Anagrafica is a choice: a persona-fisica issuer may carry only
    # Nome+Cognome (no Denominazione). The profile saves without legal_name, the
    # preview surfaces the resolved "Nome Cognome" display, but a profile with
    # NEITHER naming mode is rejected.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "A"},
            )
        ).json()
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }
        # Neither legal_name nor first+last -> rejected.
        bad = await c.post(
            "/issuer-profiles",
            headers=h,
            json={
                "label": "X",
                "vat_number": "01234567890",
                "address": "Via Roma 1",
                "postal_code": "00100",
                "city": "Roma",
            },
        )
        assert bad.status_code == 400
        # Persona fisica: only Nome+Cognome, legal_name omitted -> saved.
        p = await c.post(
            "/issuer-profiles",
            headers=h,
            json={
                "label": "Ditta",
                "first_name": "Mario",
                "last_name": "Rossi",
                "vat_number": "01234567890",
                "tax_code": "RSSMRA80A01H501U",
                "tax_regime": "RF19",
                "address": "Via Roma 1",
                "postal_code": "00100",
                "city": "Roma",
                "province": "RM",
            },
        )
        assert p.status_code == 200
        prof = p.json()
        assert prof["legal_name"] is None
        assert prof["first_name"] == "Mario" and prof["last_name"] == "Rossi"
        client = (
            await c.post(
                "/clients",
                headers=h,
                json={
                    "name": "Acme",
                    "legal_name": "Acme Srl",
                    "country_code": "IT",
                    "vat_number": "09876543210",
                    "sdi_code": "ABCDEFG",
                    "address": "Via Milano 2",
                    "postal_code": "20100",
                    "city": "Milano",
                    "province": "MI",
                },
            )
        ).json()
        inv = (
            await c.post(
                "/invoices",
                headers=h,
                json={"client_tag_id": client["id"], "year": 2026, "issuer_profile_id": prof["id"]},
            )
        ).json()
        await c.post(
            f"/invoices/{inv['id']}/lines",
            headers=h,
            json={"description": "consulting", "unit_price": "100.00"},
        )
        # Preview display name resolves to "Nome Cognome" (legal_name is empty).
        pv = (await c.get(f"/invoices/{inv['id']}/preview", headers=h)).json()
        assert pv["issuer"]["legal_name"] == "Mario Rossi"
        # The emitted XML carries Nome/Cognome, no cedente Denominazione.
        xml = (await c.get(f"/invoices/{inv['id']}/xml", headers=h)).json()["xml"]
        assert "<Nome>Mario</Nome>" in xml and "<Cognome>Rossi</Cognome>" in xml
        cedente = xml.split("<CedentePrestatore>")[1].split("</CedentePrestatore>")[0]
        assert "<Denominazione>" not in cedente
