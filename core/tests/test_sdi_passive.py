"""SdI passive cycle (services.sdi_passive): wrapper unwrap, header parse
and the inbound-endpoint router that distinguishes active notifications
from passive deliveries.

The DB-touching path (ingest_passive_invoice end-to-end) is covered by a
dedicated integration test under a tenant_session with a seeded
IssuerProfile carrying a codice destinatario.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator

import pytest

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.sdi_received import ReceivedInvoice
from mycelium_core.services import invoice as inv
from mycelium_core.services.auth import signup
from mycelium_core.services.sdi_notification_xsd import NS_MESSAGGI
from mycelium_core.services.sdi_passive import (
    PassiveDelivery,
    ingest_passive_invoice,
    ingest_receiver_notification,
    is_passive_delivery,
    is_receiver_notification,
    parse_fattura_header,
    unwrap_passive_delivery,
)

_NS = "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2"
_FATTURA_XML_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    f'<p:FatturaElettronica xmlns:p="{_NS}" versione="FPR12">'
    "<FatturaElettronicaHeader>"
    "<DatiTrasmissione>"
    "<IdTrasmittente><IdPaese>IT</IdPaese><IdCodice>09876543210</IdCodice></IdTrasmittente>"
    "<ProgressivoInvio>00001</ProgressivoInvio>"
    "<FormatoTrasmissione>FPR12</FormatoTrasmissione>"
    "<CodiceDestinatario>{codice}</CodiceDestinatario>"
    "</DatiTrasmissione>"
    "<CedentePrestatore>"
    "<DatiAnagrafici>"
    "<IdFiscaleIVA><IdPaese>IT</IdPaese><IdCodice>09876543210</IdCodice></IdFiscaleIVA>"
    "<Anagrafica><Denominazione>Fornitore SpA</Denominazione></Anagrafica>"
    "<RegimeFiscale>RF01</RegimeFiscale>"
    "</DatiAnagrafici>"
    "<Sede>"
    "<Indirizzo>Via Cedente 1</Indirizzo>"
    "<CAP>20100</CAP><Comune>Milano</Comune><Nazione>IT</Nazione>"
    "</Sede>"
    "</CedentePrestatore>"
    "</FatturaElettronicaHeader>"
    "</p:FatturaElettronica>"
)


def _fattura_xml(codice: str = "ABCDEFG") -> bytes:
    return _FATTURA_XML_TEMPLATE.format(codice=codice).encode("utf-8")


def _wrap_riceve_fatture(fattura_xml: bytes, ident: str, first_name: str) -> bytes:
    """Build the minimal SOAP-style envelope SdI uses for RiceviFatture."""
    b64 = base64.b64encode(fattura_xml).decode("ascii")
    return (
        f'<?xml version="1.0"?><RiceviFattureRequest>'
        f"<IdentificativoSdI>{ident}</IdentificativoSdI>"
        f"<NomeFile>{first_name}</NomeFile>"
        f"<File>{b64}</File>"
        f"</RiceviFattureRequest>"
    ).encode()


def test_router_passive_wrapper() -> None:
    raw = _wrap_riceve_fatture(_fattura_xml(), "SDI00000001", "IT09876543210_00001.xml")
    assert is_passive_delivery(raw) is True


def test_router_raw_fattura() -> None:
    assert is_passive_delivery(_fattura_xml()) is True


def test_router_active_notification() -> None:
    raw = b"<RicevutaConsegna><IdentificativoSdI>SDIX1</IdentificativoSdI></RicevutaConsegna>"
    assert is_passive_delivery(raw) is False


def test_router_malformed_payload() -> None:
    # Active parser will then raise ValueError -> 400 from the app.
    assert is_passive_delivery(b"") is False
    assert is_passive_delivery(b"HELLO") is False


def test_unwrap_passive_delivery_ok() -> None:
    fxml = _fattura_xml("ABCDEFG")
    raw = _wrap_riceve_fatture(fxml, "SDI00000042", "IT09876543210_00042.xml")
    d = unwrap_passive_delivery(raw)
    assert isinstance(d, PassiveDelivery)
    assert d.identificativo_sdi == "SDI00000042"
    assert d.file_name == "IT09876543210_00042.xml"
    assert d.fattura_xml == fxml


def test_unwrap_rejects_missing_fields() -> None:
    with pytest.raises(ValueError):
        unwrap_passive_delivery(b"<RiceviFattureRequest></RiceviFattureRequest>")


def test_parse_fattura_header_ok() -> None:
    h = parse_fattura_header(_fattura_xml("ABCDEFG"))
    assert h.transmission_format == "FPR12"
    assert h.sender_country_code == "IT"
    assert h.sender_vat_number == "09876543210"
    assert h.sender_legal_name == "Fornitore SpA"
    assert h.sdi_code == "ABCDEFG"


def test_parse_fattura_rejects_non_fattura_root() -> None:
    with pytest.raises(ValueError):
        parse_fattura_header(b"<NotAFattura/>")


def test_parse_fattura_rejects_missing_codice_destinatario() -> None:
    # Strip the codice destinatario line from the template.
    xml = _fattura_xml().replace(b"<CodiceDestinatario>ABCDEFG</CodiceDestinatario>", b"")
    with pytest.raises(ValueError):
        parse_fattura_header(xml)


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="PASSIVE",
        )
    return r.org_id, r.user_id


@pytest.fixture
def _seeded_recipient() -> Iterator[None]:
    # Nothing to set/tear down here; the integration test seeds its own
    # IssuerProfile because each test wants a unique codice destinatario.
    yield


async def test_ingest_passive_invoice_resolves_recipient_and_stores(
    _seeded_recipient: None,
) -> None:
    org, user = await _org()
    codice = uuid.uuid4().hex[:7].upper()  # unique 7-char codice for the test
    async with tenant_session(str(org), str(user)) as s:
        issuer = await inv.create_issuer_profile(
            s,
            org_id=org,
            actor_id=user,
            label="Recipient",
            legal_name="Recipient SRL",
            vat_number="01112223334",
            address="Via Test 1",
            postal_code="10100",
            city="Torino",
            is_default=True,
        )
        # Seed the codice destinatario (column added in migration 0102).
        issuer.sdi_code = codice
        await s.flush()

    raw = _wrap_riceve_fatture(
        _fattura_xml(codice),
        ident=f"PSV{uuid.uuid4().hex[:8].upper()}",
        first_name=f"IT09876543210_{uuid.uuid4().hex[:5].upper()}.xml",
    )
    stored = await ingest_passive_invoice(raw)
    assert stored is not None
    assert stored.org_id == org
    assert stored.issuer_profile_id == issuer.id
    assert stored.sdi_code == codice
    assert stored.processing_status == "new"
    assert stored.sender_vat_number == "09876543210"
    # Idempotency: a second push with the same IdentificativoSdI does not
    # double-insert.
    again = await ingest_passive_invoice(raw)
    assert again is not None
    assert again.id == stored.id
    # Confirm via a fresh tenant session there is only one row.
    async with tenant_session(str(org), str(user)) as s:
        from sqlalchemy import func, select

        count = (
            await s.execute(
                select(func.count())
                .select_from(ReceivedInvoice)
                .where(ReceivedInvoice.identificativo_sdi == stored.identificativo_sdi)
            )
        ).scalar_one()
    assert count == 1


async def test_ingest_passive_invoice_orphan_returns_none() -> None:
    # No IssuerProfile owns this codice destinatario -> the inbound logs
    # the orphan and answers 200; the service layer signals None so the
    # endpoint does not crash.
    raw = _wrap_riceve_fatture(
        _fattura_xml("ZZZ0000"),
        ident=f"PSV{uuid.uuid4().hex[:8].upper()}",
        first_name="IT09876543210_ZZZ.xml",
    )
    assert await ingest_passive_invoice(raw) is None


# --- receiver-cycle notifications (MT / SE) -----------------------------------


def _mt(ident: str, *, message_id: str = "MID00MT1") -> bytes:
    return (
        f'<m:MetadatiInvioFile xmlns:m="{NS_MESSAGGI}" versione="1.0">'
        f"<IdentificativoSdI>{ident}</IdentificativoSdI>"
        f"<NomeFile>IT09876543210_00001.xml</NomeFile>"
        f"<CodiceDestinatario>ABCDEFG</CodiceDestinatario>"
        f"<Formato>FPR12</Formato>"
        f"<TentativiInvio>1</TentativiInvio>"
        f"<MessageId>{message_id}</MessageId>"
        f"</m:MetadatiInvioFile>"
    ).encode()


def _se(ident: str, *, message_id: str = "MID00SE1") -> bytes:
    return (
        f'<m:ScartoEsitoCommittente xmlns:m="{NS_MESSAGGI}" versione="1.0">'
        f"<IdentificativoSdI>{ident}</IdentificativoSdI>"
        f"<Scarto>EN00</Scarto>"
        f"<MessageId>{message_id}</MessageId>"
        f"</m:ScartoEsitoCommittente>"
    ).encode()


def test_router_classifies_receiver_notifications() -> None:
    # MT / SE must take the receiver path, not the passive (FatturaElettronica)
    # one. is_passive_delivery has historically false-positived on anything
    # carrying a NomeFile; that bug is fixed and explicitly guarded here.
    assert is_passive_delivery(_mt("100000000001")) is False
    assert is_passive_delivery(_se("100000000002")) is False
    assert is_receiver_notification(_mt("100000000001")) is True
    assert is_receiver_notification(_se("100000000002")) is True


def test_router_does_not_misclassify_active_notification_with_nomefile() -> None:
    # Regression: a real RC has NomeFile; it must not route to the passive
    # path just because that element is present.
    rc = (
        f'<m:RicevutaConsegna xmlns:m="{NS_MESSAGGI}" versione="1.0">'
        f"<IdentificativoSdI>100000000001</IdentificativoSdI>"
        f"<NomeFile>x.xml</NomeFile>"
        f"<DataOraRicezione>2026-05-25T10:00:00</DataOraRicezione>"
        f"<DataOraConsegna>2026-05-25T10:01:00</DataOraConsegna>"
        f"<Destinatario><Codice>ABCDEFG</Codice><Descrizione>A</Descrizione></Destinatario>"
        f"<MessageId>M</MessageId>"
        f"</m:RicevutaConsegna>"
    ).encode()
    assert is_passive_delivery(rc) is False
    assert is_receiver_notification(rc) is False


async def test_ingest_receiver_notification_appends_audit_row(
    _seeded_recipient: None,
) -> None:
    # Spin up an issuer + received_invoices row, then feed a MT/SE notification
    # carrying the same IdentificativoSdI; both write a row on
    # received_invoice_notifications, dedup-unique on (kind, message_id).
    org, user = await _org()
    codice = uuid.uuid4().hex[:7].upper()
    async with tenant_session(str(org), str(user)) as s:
        issuer = await inv.create_issuer_profile(
            s,
            org_id=org,
            actor_id=user,
            label="R",
            legal_name="Recipient SRL",
            vat_number="01112223334",
            address="Via Test 1",
            postal_code="10100",
            city="Torino",
            is_default=True,
        )
        issuer.sdi_code = codice
        await s.flush()

    # The receiver-cycle XSD restricts IdentificativoSdI to xsd:integer
    # (<=12 digits); pick a numeric id for the delivery so the MT/SE that
    # follow can refer to the same row through schema-valid notifications.
    ident = str(int(uuid.uuid4().hex[:9], 16) % 10**12)
    delivery = _wrap_riceve_fatture(
        _fattura_xml(codice),
        ident=ident,
        first_name=f"IT09876543210_{uuid.uuid4().hex[:5].upper()}.xml",
    )
    ri = await ingest_passive_invoice(delivery)
    assert ri is not None

    # MT and SE on the same received invoice.
    mt_result = await ingest_receiver_notification(_mt(ident))
    assert mt_result is not None and mt_result.id == ri.id
    se_result = await ingest_receiver_notification(_se(ident))
    assert se_result is not None and se_result.id == ri.id

    # SdI retry of MT with the same MessageId is a no-op (dedupe index).
    await ingest_receiver_notification(_mt(ident))

    async with tenant_session(str(org), str(user)) as s:
        from sqlalchemy import select

        from mycelium_core.models.sdi_notification import ReceivedInvoiceNotification

        stmt = select(ReceivedInvoiceNotification.kind).order_by(
            ReceivedInvoiceNotification.received_at
        )
        kinds = (await s.execute(stmt)).scalars().all()
    assert kinds == ["MT", "SE"]


async def test_ingest_receiver_notification_orphan_returns_none() -> None:
    # No received_invoices row matches this IdentificativoSdI -> 200 + None,
    # so SdI does not flood the retry queue.
    assert await ingest_receiver_notification(_mt("999999999999")) is None
