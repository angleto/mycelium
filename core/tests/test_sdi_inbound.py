"""SdI inbound notification ingest (F7b): namespace-agnostic parsing and the
cross-org correlation (resolve the tenant by IdentificativoSdI with no tenant
context, via the SECURITY DEFINER resolver, then apply the outcome).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest

from flow_core.db import admin_session, tenant_session
from flow_core.models.invoice import ConservationStatus, InvoiceState, SdiStatus
from flow_core.sdi_channel import IntermediaryIdentity, TransmitResult, set_channel_override
from flow_core.services import invoice as inv
from flow_core.services import sdi_mandate as mandate
from flow_core.services.auth import signup
from flow_core.services.sdi_inbound import ingest_notification, parse_notification
from flow_core.services.taxonomy import ClientInput, create_client


def test_parse_ricevuta_consegna() -> None:
    raw = b"<RicevutaConsegna><IdentificativoSdI>SDIX1</IdentificativoSdI></RicevutaConsegna>"
    assert parse_notification(raw) == ("SDIX1", "RC")


def test_parse_scarto_with_namespace() -> None:
    raw = (
        b'<ns:NotificaScarto xmlns:ns="urn:x">'
        b"<IdentificativoSdI>SDIX2</IdentificativoSdI></ns:NotificaScarto>"
    )
    assert parse_notification(raw) == ("SDIX2", "NS")


def test_parse_unknown_notification_raises() -> None:
    with pytest.raises(ValueError):
        parse_notification(b"<Foo><IdentificativoSdI>X</IdentificativoSdI></Foo>")


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="INB",
        )
    return r.org_id, r.user_id


@pytest.fixture
def _coop() -> Iterator[None]:
    class FakeCoop:
        name = "sdicoop"

        @property
        def intermediary(self) -> IntermediaryIdentity | None:
            return IntermediaryIdentity(
                id_paese="IT", id_codice="11122233344", denominazione="Flow Intermediary Srl"
            )

        async def transmit(self, *, xml: str, invoice_id: str, filename: str) -> TransmitResult:
            # Unique per invoice so concurrent/repeated test runs never collide.
            return TransmitResult(
                identificativo_sdi=f"SDIINB{invoice_id[:8].upper()}",
                conservation=ConservationStatus.ade_pending,
                channel=self.name,
            )

    set_channel_override(FakeCoop)
    try:
        yield
    finally:
        set_channel_override(None)


async def test_inbound_ingest_correlates_cross_org_and_marks_delivered(_coop: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        issuer = await inv.create_issuer_profile(
            s,
            org_id=org,
            actor_id=user,
            label="P",
            denominazione="Acme Srl",
            piva="01234567890",
            indirizzo="Via Roma 1",
            cap="00100",
            comune="Roma",
            is_default=True,
        )
        client = await create_client(
            s,
            org_id=org,
            actor_id=user,
            name="C",
            profile=ClientInput(
                ragione_sociale="Client SpA",
                id_paese="IT",
                id_codice="09876543210",
                codice_destinatario="ABCDEFG",
                indirizzo="Via Milano 2",
                cap="20100",
                comune="Milano",
            ),
        )
        await mandate.grant_mandate(s, org_id=org, actor_id=user, issuer_profile_id=issuer.id)
        d = await inv.create_draft(s, org_id=org, actor_id=user, client_tag_id=client.id, year=2026)
        await inv.add_line(
            s,
            org_id=org,
            actor_id=user,
            invoice_id=d.id,
            description="svc",
            unit_price=Decimal(100),
        )
        tx = await inv.transmit(s, org_id=org, actor_id=user, invoice_id=d.id)
        identificativo = tx.identificativo_sdi
    assert identificativo is not None

    # Ingest a delivery receipt with NO tenant context: the service must
    # resolve the org cross-org by IdentificativoSdI, then apply the outcome.
    raw = (
        f"<RicevutaConsegna><IdentificativoSdI>{identificativo}</IdentificativoSdI>"
        f"</RicevutaConsegna>"
    ).encode()
    updated = await ingest_notification(raw)
    assert updated is not None
    assert updated.sdi_status is SdiStatus.RC
    assert updated.state is InvoiceState.delivered
    assert updated.conservation_status is ConservationStatus.ade_covered

    # An unknown IdentificativoSdI yields None (SdI may retry; never a 500).
    unknown = b"<RicevutaConsegna><IdentificativoSdI>NOPE000</IdentificativoSdI></RicevutaConsegna>"
    assert await ingest_notification(unknown) is None
