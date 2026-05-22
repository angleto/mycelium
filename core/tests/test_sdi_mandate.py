"""SdI transmission mandate (ADR-0011): grant/revoke lifecycle and the
transmit gate (the intermediary channel refuses to send without an active
mandate for the invoice's VAT subject).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest

from flow_core.db import admin_session, tenant_session
from flow_core.errors import ConflictError, NotFoundError
from flow_core.models.invoice import ConservationStatus
from flow_core.models.sdi_mandate import SdiMandateStatus
from flow_core.sdi_channel import IntermediaryIdentity, TransmitResult, set_channel_override
from flow_core.services import invoice as inv
from flow_core.services import sdi_mandate as mandate
from flow_core.services.auth import signup
from flow_core.services.taxonomy import ClientInput, create_client


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MND",
        )
    return r.org_id, r.user_id


async def _issuer_and_client(s, org: uuid.UUID, user: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    p = await inv.create_issuer_profile(
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
    c = await create_client(
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
            provincia="MI",
        ),
    )
    return p.id, c.id


async def test_grant_is_idempotent_and_revoke_flips_status() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        issuer_id, _ = await _issuer_and_client(s, org, user)
        m1 = await mandate.grant_mandate(
            s, org_id=org, actor_id=user, issuer_profile_id=issuer_id, reference="contract-1"
        )
        assert m1.status is SdiMandateStatus.active
        # Re-granting an active mandate is idempotent (same row).
        m2 = await mandate.grant_mandate(s, org_id=org, actor_id=user, issuer_profile_id=issuer_id)
        assert m2.id == m1.id
        active = await mandate.get_active_mandate(s, org_id=org, issuer_profile_id=issuer_id)
        assert active is not None and active.id == m1.id
        revoked = await mandate.revoke_mandate(
            s, org_id=org, actor_id=user, issuer_profile_id=issuer_id
        )
        assert revoked.status is SdiMandateStatus.revoked and revoked.revoked_at is not None
        assert await mandate.get_active_mandate(s, org_id=org, issuer_profile_id=issuer_id) is None
        with pytest.raises(NotFoundError):
            await mandate.revoke_mandate(s, org_id=org, actor_id=user, issuer_profile_id=issuer_id)


@pytest.fixture
def _intermediary_channel() -> Iterator[None]:
    class FakeCoop:
        name = "sdicoop"

        @property
        def intermediary(self) -> IntermediaryIdentity | None:
            return IntermediaryIdentity(
                id_paese="IT", id_codice="11122233344", denominazione="Flow Intermediary Srl"
            )

        async def transmit(self, *, xml: str, invoice_id: str, filename: str) -> TransmitResult:
            return TransmitResult(
                identificativo_sdi="SDIFAKE000001",
                conservation=ConservationStatus.ade_pending,
                channel=self.name,
            )

    set_channel_override(FakeCoop)
    try:
        yield
    finally:
        set_channel_override(None)


async def test_transmit_via_intermediary_requires_mandate(_intermediary_channel: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        issuer_id, client_id = await _issuer_and_client(s, org, user)
        d = await inv.create_draft(s, org_id=org, actor_id=user, client_tag_id=client_id, year=2026)
        await inv.add_line(
            s,
            org_id=org,
            actor_id=user,
            invoice_id=d.id,
            description="svc",
            unit_price=Decimal(100),
        )
        # No mandate yet: the intermediary path is blocked (no number burned).
        with pytest.raises(ConflictError):
            await inv.transmit(s, org_id=org, actor_id=user, invoice_id=d.id)
        assert d.number is None
        # After granting, transmit succeeds and stamps the intermediary block.
        await mandate.grant_mandate(s, org_id=org, actor_id=user, issuer_profile_id=issuer_id)
        tx = await inv.transmit(s, org_id=org, actor_id=user, invoice_id=d.id)
        assert tx.identificativo_sdi == "SDIFAKE000001"
        assert "<TerzoIntermediarioOSoggettoEmittente>" in (tx.xml or "")
        assert "<SoggettoEmittente>TZ</SoggettoEmittente>" in (tx.xml or "")
        # The intermediary is the trasmittente (IdTrasmittente), not the cedente.
        assert "<IdCodice>11122233344</IdCodice>" in (tx.xml or "")
