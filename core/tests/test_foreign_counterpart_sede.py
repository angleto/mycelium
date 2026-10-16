"""A foreign counterpart's Sede as FatturaPA wants it (AdE FAQ 63 and 74).

``CAPType`` is five digits and mandatory, so a real foreign postal code can
never be written in ``CAP``: the convention is ``00000``, the foreign code may
travel in ``Indirizzo``, and ``Provincia`` is not needed. Two documents for
Swiss counterparts failed the XSD in production on 2026-10-16 with their real
four-digit codes; the first test was observed failing on that code before the
fix, with the same ``CAP`` pattern error.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import Decimal

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.services import invoice as inv_svc
from mycelium_core.services import taxonomy
from mycelium_core.services.auth import signup
from mycelium_core.services.taxonomy import ClientInput


async def _xml_for(profile: ClientInput) -> str:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="Foreign",
        )
    async with tenant_session(str(r.org_id), str(r.user_id)) as s:
        await inv_svc.create_issuer_profile(
            s,
            org_id=r.org_id,
            actor_id=r.user_id,
            label="Principale",
            legal_name="HahnBanach SRL",
            vat_number="01234567890",
            address="Via Roma",
            civic_number="1",
            postal_code="00100",
            city="Roma",
            province="RM",
            is_default=True,
        )
        tag = await taxonomy.create_client(
            s, org_id=r.org_id, actor_id=r.user_id, name="Cliente", profile=profile
        )
        inv = await inv_svc.create_draft(
            s, org_id=r.org_id, actor_id=r.user_id, client_tag_id=tag.id, purpose="Servizio"
        )
        await inv_svc.add_line(
            s,
            org_id=r.org_id,
            actor_id=r.user_id,
            invoice_id=inv.id,
            description="Servizio",
            unit_price=Decimal(100),
            quantity=Decimal(1),
            vat_rate=Decimal(0),
            vat_nature="N2.1",
        )
        return await inv_svc.get_xml_preview(s, org_id=r.org_id, invoice_id=inv.id)


def _sede(xml: str) -> str:
    cess = xml.split("<CessionarioCommittente>")[1].split("</CessionarioCommittente>")[0]
    return cess.split("<Sede>")[1].split("</Sede>")[0]


_SWISS = ClientInput(
    legal_name="Muster SA",
    country_code="CH",
    vat_number="CHE123456789",
    address="Via Nassa 5",
    postal_code="6900",
    city="Lugano",
    province="TI",
    country="CH",
    sdi_code="XXXXXXX",
)


async def test_a_swiss_postal_code_becomes_the_conventional_cap() -> None:
    sede = _sede(await _xml_for(_SWISS))
    assert "<CAP>00000</CAP>" in sede
    assert "<Indirizzo>Via Nassa 5 6900</Indirizzo>" in sede
    assert "<Provincia>" not in sede
    assert "<Nazione>CH</Nazione>" in sede


async def test_a_foreign_counterpart_without_a_postal_code_is_still_emittable() -> None:
    sede = _sede(await _xml_for(replace(_SWISS, postal_code=None)))
    assert "<CAP>00000</CAP>" in sede
    assert "<Indirizzo>Via Nassa 5</Indirizzo>" in sede


async def test_an_italian_counterpart_keeps_its_own_cap_and_province() -> None:
    sede = _sede(
        await _xml_for(
            ClientInput(
                legal_name="Acme SpA",
                country_code="IT",
                vat_number="09876543210",
                address="Via Milano 9",
                postal_code="20100",
                city="Milano",
                province="MI",
                country="IT",
                sdi_code="ABCDEFG",
            )
        )
    )
    assert "<CAP>20100</CAP>" in sede
    assert "<Indirizzo>Via Milano 9</Indirizzo>" in sede
    assert "<Provincia>MI</Provincia>" in sede
