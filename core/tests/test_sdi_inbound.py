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
from flow_core.services.sdi_notification_xsd import NS_MESSAGGI
from flow_core.services.taxonomy import ClientInput, create_client

# v1 notification fixtures are XSD-valid against MessaggiTypes_v1.1: SdI
# rejects anything else, and so do we. The root carries the official messaggi
# namespace; children are unqualified (the schema is elementFormDefault
# unqualified). ``ds:Signature`` is omitted on purpose -- the validator is
# signature-relaxed (XAdES verification is a separate, post-v1 concern).


def _rc(ident: str) -> bytes:
    return (
        f'<m:RicevutaConsegna xmlns:m="{NS_MESSAGGI}" versione="1.0">'
        f"<IdentificativoSdI>{ident}</IdentificativoSdI>"
        f"<NomeFile>IT01234567890_00001.xml</NomeFile>"
        f"<DataOraRicezione>2026-05-25T10:00:00</DataOraRicezione>"
        f"<DataOraConsegna>2026-05-25T10:01:00</DataOraConsegna>"
        f"<Destinatario><Codice>ABCDEFG</Codice><Descrizione>Acme</Descrizione></Destinatario>"
        f"<MessageId>MID00001</MessageId>"
        f"</m:RicevutaConsegna>"
    ).encode()


def _ns(ident: str) -> bytes:
    return (
        f'<m:NotificaScarto xmlns:m="{NS_MESSAGGI}" versione="1.0">'
        f"<IdentificativoSdI>{ident}</IdentificativoSdI>"
        f"<NomeFile>IT01234567890_00001.xml</NomeFile>"
        f"<DataOraRicezione>2026-05-25T10:00:00</DataOraRicezione>"
        f"<ListaErrori><Errore><Codice>00001</Codice>"
        f"<Descrizione>boom</Descrizione></Errore></ListaErrori>"
        f"<MessageId>MID00002</MessageId>"
        f"</m:NotificaScarto>"
    ).encode()


def test_parse_ricevuta_consegna() -> None:
    assert parse_notification(_rc("100000000001")) == ("100000000001", "RC")


def test_parse_notifica_scarto() -> None:
    assert parse_notification(_ns("100000000002")) == ("100000000002", "NS")


def test_parse_rejects_lax_namespace() -> None:
    # Bare XML without the official messaggi namespace is rejected: SdI never
    # emits a notification without the canonical namespace; tolerating that
    # would mask a real protocol bug.
    bare = b"<RicevutaConsegna><IdentificativoSdI>1</IdentificativoSdI></RicevutaConsegna>"
    with pytest.raises(ValueError, match="MessaggiTypes"):
        parse_notification(bare)


def test_parse_rejects_unknown_root() -> None:
    foo = f'<m:Foo xmlns:m="{NS_MESSAGGI}"/>'.encode()
    with pytest.raises(ValueError, match="MessaggiTypes"):
        parse_notification(foo)


def test_parse_rejects_missing_required_field() -> None:
    # A RicevutaConsegna missing DataOraConsegna is structurally invalid; the
    # XSD gate must reject it before XPath extraction.
    incomplete = (
        f'<m:RicevutaConsegna xmlns:m="{NS_MESSAGGI}" versione="1.0">'
        f"<IdentificativoSdI>1</IdentificativoSdI>"
        f"<NomeFile>x.xml</NomeFile>"
        f"<DataOraRicezione>2026-05-25T10:00:00</DataOraRicezione>"
        f"<Destinatario><Codice>ABCDEFG</Codice><Descrizione>A</Descrizione></Destinatario>"
        f"<MessageId>M</MessageId>"
        f"</m:RicevutaConsegna>"
    ).encode()
    with pytest.raises(ValueError, match="MessaggiTypes"):
        parse_notification(incomplete)


def test_inbound_app_rejects_malformed_xml_with_400() -> None:
    # ADR-0011: SdI push must never see a 500 (its retry/log path gets
    # noisy). Both an empty body and arbitrary non-XML must surface as 400,
    # not bubble lxml.XMLSyntaxError into a 500.
    from fastapi.testclient import TestClient

    from flow_sdi_inbound.app import create_app

    client = TestClient(create_app())
    assert client.post("/sdi/notification", content=b"").status_code == 400
    assert client.post("/sdi/notification", content=b"HELLO").status_code == 400


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
            # IdentificativoSdI is xsd:integer with up to 12 digits per the
            # official schema; derive a unique numeric id from the invoice
            # UUID so concurrent/repeated test runs never collide.
            numeric = int(invoice_id.replace("-", "")[:11], 16) % 10**12
            return TransmitResult(
                identificativo_sdi=str(numeric),
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
    updated = await ingest_notification(_rc(identificativo))
    assert updated is not None
    assert updated.sdi_status is SdiStatus.RC
    assert updated.state is InvoiceState.delivered
    assert updated.conservation_status is ConservationStatus.ade_covered

    # An unknown IdentificativoSdI yields None (SdI may retry; never a 500).
    assert await ingest_notification(_rc("999999999999")) is None
