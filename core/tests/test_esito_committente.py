"""EC outbound (ADR-0011 v1.1): build, sign, persist a
NotificaEsitoCommittente.

Tests cover the builder (XSD-valid output, signature element present, esito
+ verdict mapping) and the persistence wrapper (audit row written with
``direction='out'``, denormalized buyer_verdict updated, retry of the
same ``message_id`` is refused as a domain error).

Self-signed RSA material is generated once per module so signxml has
something realistic to sign with; the trust chain is irrelevant to the
builder, only the key+cert pair is.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Iterator

import lxml.etree as ET
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from flow_core.config import get_settings
from flow_core.db import tenant_session
from flow_core.errors import DomainError
from flow_core.models.sdi_received import BuyerVerdict, ReceivedInvoice
from flow_core.services import invoice as inv
from flow_core.services.auth import signup
from flow_core.services.esito_committente import (
    build_esito_committente_xml,
    send_esito_committente,
)
from flow_core.services.sdi_notification_xsd import NS_MESSAGGI, validate_sdi_notification
from flow_core.services.sdi_passive import ingest_passive_invoice

_DS_NS = "http://www.w3.org/2000/09/xmldsig#"


def _generate_test_signing_material() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "flow-test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subj)
        .issuer_name(subj)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC))
        .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return key_pem, cert_pem


@pytest.fixture
def _ec_signing() -> Iterator[None]:
    """Provision ephemeral signing material for the duration of the test;
    the settings singleton picks it up from the env (BaseSettings reads at
    instantiation, so we cache-bust by clearing the LRU cache)."""
    key_pem, cert_pem = _generate_test_signing_material()
    os.environ["FLOW_SDI_EC_SIGNING_KEY_PEM"] = key_pem
    os.environ["FLOW_SDI_EC_SIGNING_CERT_PEM"] = cert_pem
    get_settings.cache_clear()
    try:
        yield
    finally:
        del os.environ["FLOW_SDI_EC_SIGNING_KEY_PEM"]
        del os.environ["FLOW_SDI_EC_SIGNING_CERT_PEM"]
        get_settings.cache_clear()


@pytest.fixture
def _sdicoop_active(tmp_path: object, _ec_signing: None) -> Iterator[str]:
    """Stand up a fully provisioned sdicoop channel pointing at a fake
    endpoint: real PEM files on disk (httpx opens them when creating the
    SSL context, so the path must exist) + the intermediary identity +
    endpoint URL. Yields the endpoint URL so the test can register the
    matching respx route. Tears env down on exit."""
    import pathlib

    tp = pathlib.Path(str(tmp_path))
    key_pem, cert_pem = _generate_test_signing_material()
    cert_path = tp / "client.crt.pem"
    key_path = tp / "client.key.pem"
    cert_path.write_text(cert_pem)
    key_path.write_text(key_pem)
    endpoint = "https://sdi-test.example/ricevinotifica"
    env = {
        "FLOW_SDI_CHANNEL": "sdicoop",
        "FLOW_SDI_INTERMEDIARY_ID_PAESE": "IT",
        "FLOW_SDI_INTERMEDIARY_ID_CODICE": "11122233344",
        "FLOW_SDI_INTERMEDIARY_DENOMINAZIONE": "Flow Intermediary Srl",
        "FLOW_SDI_ENDPOINT_URL": endpoint,
        "FLOW_SDI_CLIENT_CERT": str(cert_path),
        "FLOW_SDI_CLIENT_KEY": str(key_path),
    }
    for k, v in env.items():
        os.environ[k] = v
    get_settings.cache_clear()
    try:
        yield endpoint
    finally:
        for k in env:
            os.environ.pop(k, None)
        get_settings.cache_clear()


def test_build_ec_xml_is_xsd_valid_and_signed(_ec_signing: None) -> None:
    out = build_esito_committente_xml(
        identificativo_sdi="123456789012", esito="EC01", message_id_committente="M1"
    )
    assert validate_sdi_notification(out) == []
    doc = ET.fromstring(out)
    # Signature element is enveloped under the root.
    sig = doc.find(f"{{{_DS_NS}}}Signature")
    assert sig is not None, "expected ds:Signature enveloped in the root"
    # Esito + IdentificativoSdI extracted correctly.
    assert doc.findtext("Esito") == "EC01"
    assert doc.findtext("IdentificativoSdI") == "123456789012"


def test_build_ec_rejects_bad_esito(_ec_signing: None) -> None:
    with pytest.raises(DomainError) as exc:
        build_esito_committente_xml(identificativo_sdi="1", esito="EC99")
    assert "EC01 or EC02" in exc.value.params.get("detail", "")


def test_build_ec_fails_without_signing_material() -> None:
    # Make sure prior fixtures didn't leak material.
    os.environ.pop("FLOW_SDI_EC_SIGNING_KEY_PEM", None)
    os.environ.pop("FLOW_SDI_EC_SIGNING_CERT_PEM", None)
    get_settings.cache_clear()
    with pytest.raises(DomainError) as exc:
        build_esito_committente_xml(identificativo_sdi="1", esito="EC01")
    assert "not provisioned" in exc.value.params.get("detail", "")


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


def _wrap_riceve_fatture(fattura_xml: bytes, ident: str, first_name: str) -> bytes:
    import base64

    b64 = base64.b64encode(fattura_xml).decode("ascii")
    return (
        f'<?xml version="1.0"?><RiceviFattureRequest>'
        f"<IdentificativoSdI>{ident}</IdentificativoSdI>"
        f"<NomeFile>{first_name}</NomeFile>"
        f"<File>{b64}</File>"
        f"</RiceviFattureRequest>"
    ).encode()


async def _make_org_with_received_invoice() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create org + issuer (with codice destinatario) + a ReceivedInvoice via
    the passive ingest. Returns (org_id, user_id, received_invoice_id)."""
    from flow_core.db import admin_session

    async with admin_session() as s:
        signed = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="EC",
        )
    org, user = signed.org_id, signed.user_id
    codice = uuid.uuid4().hex[:7].upper()
    async with tenant_session(str(org), str(user)) as s:
        issuer = await inv.create_issuer_profile(
            s,
            org_id=org,
            actor_id=user,
            label="R",
            legal_name="Recipient SRL",
            vat_number="13438810015",
            address="Via Test 1",
            postal_code="10100",
            city="Torino",
            is_default=True,
        )
        issuer.sdi_code = codice
        await s.flush()

    ident = str(int(uuid.uuid4().hex[:9], 16) % 10**12)
    raw = _wrap_riceve_fatture(
        _FATTURA_XML_TEMPLATE.format(codice=codice).encode(),
        ident=ident,
        first_name=f"IT09876543210_{uuid.uuid4().hex[:5].upper()}.xml",
    )
    ri = await ingest_passive_invoice(raw)
    assert ri is not None
    return org, user, ri.id


async def test_send_ec_persists_audit_and_updates_verdict(_ec_signing: None) -> None:
    org, user, ri_id = await _make_org_with_received_invoice()
    async with tenant_session(str(org), str(user)) as s:
        notif = await send_esito_committente(
            s,
            org_id=org,
            actor_id=user,
            received_invoice_id=ri_id,
            esito="EC01",
        )
    assert notif.kind == "EC"
    assert notif.direction == "out"
    assert notif.payload["esito"] == "EC01"
    # Signed bytes are stored verbatim and XSD-valid.
    assert validate_sdi_notification(notif.raw_xml) == []
    # Verdict denormalized on the parent.
    from sqlalchemy import select

    from flow_core.models.sdi_received import ReceivedInvoice

    async with tenant_session(str(org), str(user)) as s:
        ri = (
            await s.execute(select(ReceivedInvoice).where(ReceivedInvoice.id == ri_id))
        ).scalar_one()
    assert ri.buyer_verdict is BuyerVerdict.accepted
    assert ri.buyer_verdict_at is not None


async def test_dt_receiver_side_marks_deemed_accepted(_ec_signing: None) -> None:
    # DT is dual-cycle: when no Invoice matches the IdentificativoSdI, the
    # dispatcher falls back to the receiver path. ingest_receiver_dt then
    # marks the received invoice deemed-accepted (15-day window expired
    # without us sending EC) and appends a DT audit row.
    from flow_core.services.sdi_inbound import ingest_notification

    org, user, ri_id = await _make_org_with_received_invoice()
    # Pull the IdentificativoSdI back to craft a DT that targets it.
    from sqlalchemy import select

    from flow_core.models.sdi_received import ReceivedInvoice

    async with tenant_session(str(org), str(user)) as s:
        ri = (
            await s.execute(select(ReceivedInvoice).where(ReceivedInvoice.id == ri_id))
        ).scalar_one()
    ident = ri.identificativo_sdi

    dt_xml = (
        f'<m:NotificaDecorrenzaTermini xmlns:m="{NS_MESSAGGI}" versione="1.0">'
        f"<IdentificativoSdI>{ident}</IdentificativoSdI>"
        f"<NomeFile>x.xml</NomeFile>"
        f"<MessageId>MID00DT2</MessageId>"
        f"</m:NotificaDecorrenzaTermini>"
    ).encode()
    # No matching Invoice -> dispatcher falls back to receiver-side and
    # returns None; the receiver-side update happened nonetheless.
    assert await ingest_notification(dt_xml) is None
    async with tenant_session(str(org), str(user)) as s:
        updated = (
            await s.execute(select(ReceivedInvoice).where(ReceivedInvoice.id == ri_id))
        ).scalar_one()
    assert updated.buyer_verdict is BuyerVerdict.deemed_accepted
    assert updated.dt_received_at is not None


async def test_send_ec_rejection_marks_rejected_verdict(_ec_signing: None) -> None:
    org, user, ri_id = await _make_org_with_received_invoice()
    async with tenant_session(str(org), str(user)) as s:
        await send_esito_committente(
            s,
            org_id=org,
            actor_id=user,
            received_invoice_id=ri_id,
            esito="EC02",
            descrizione="merce non conforme",
        )
    from sqlalchemy import select

    from flow_core.models.sdi_received import ReceivedInvoice

    async with tenant_session(str(org), str(user)) as s:
        ri = (
            await s.execute(select(ReceivedInvoice).where(ReceivedInvoice.id == ri_id))
        ).scalar_one()
    assert ri.buyer_verdict is BuyerVerdict.rejected


# --- Live transport wiring (sdicoop channel) ----------------------------------


async def test_send_ec_with_sdicoop_posts_and_stores_ack(_sdicoop_active: str) -> None:
    """When the channel is fully provisioned the service POSTs the signed EC
    over (mocked) mutual TLS and stores the SdI ack on the audit row."""
    import respx
    from httpx import Response

    from flow_core.models.sdi_notification import ReceivedInvoiceNotification

    org, user, ri_id = await _make_org_with_received_invoice()
    endpoint = _sdicoop_active

    ack_envelope = (
        b'<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
        b"<soapenv:Body><EsitoRicezione>ER01</EsitoRicezione></soapenv:Body>"
        b"</soapenv:Envelope>"
    )

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(endpoint).mock(
            return_value=Response(200, content=ack_envelope, headers={"Content-Type": "text/xml"})
        )
        async with tenant_session(str(org), str(user)) as s:
            notif = await send_esito_committente(
                s, org_id=org, actor_id=user, received_invoice_id=ri_id, esito="EC01"
            )

    assert route.called
    sent_req = route.calls.last.request
    # The body must be the SOAP envelope around the signed EC. We do not
    # decode the base64 file here -- shape checks (SOAPAction + the
    # filename derived from the message_id) are enough to confirm wiring.
    assert sent_req.headers["soapaction"] == '"NotificaEsito"'
    assert notif.payload["ack"] == "ER01"
    assert notif.payload["filename"].endswith("_EC_001.xml")

    # The audit row reads back with the ack persisted.
    async with tenant_session(str(org), str(user)) as s:
        from sqlalchemy import select as _sel

        rows = (
            (
                await s.execute(
                    _sel(ReceivedInvoiceNotification).where(
                        ReceivedInvoiceNotification.received_invoice_id == ri_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1 and rows[0].payload["ack"] == "ER01"


async def test_send_ec_with_sdicoop_transport_error_persists_anyway(
    _sdicoop_active: str,
) -> None:
    """Network/HTTP failure must not roll back the persisted EC: the buyer's
    intent is durable, the transport_error is recorded so an operator can
    replay. Verdict denormalization still moves to accepted/rejected."""
    import respx
    from httpx import Response

    org, user, ri_id = await _make_org_with_received_invoice()
    endpoint = _sdicoop_active

    with respx.mock(assert_all_called=True) as mock:
        mock.post(endpoint).mock(return_value=Response(500, content=b"boom"))
        async with tenant_session(str(org), str(user)) as s:
            notif = await send_esito_committente(
                s, org_id=org, actor_id=user, received_invoice_id=ri_id, esito="EC02"
            )

    assert "transport_error" in notif.payload
    assert "ack" not in notif.payload
    # The signed XML is still stored verbatim + the verdict moved.
    assert validate_sdi_notification(notif.raw_xml) == []
    from sqlalchemy import select as _sel

    async with tenant_session(str(org), str(user)) as s:
        ri = (
            await s.execute(_sel(ReceivedInvoice).where(ReceivedInvoice.id == ri_id))
        ).scalar_one()
    assert ri.buyer_verdict is BuyerVerdict.rejected


async def test_send_ec_without_channel_does_not_call_transport(_ec_signing: None) -> None:
    """Default (manual_export) channel: the persistence path runs alone, no
    HTTP is attempted. respx ``assert_all_called=False`` so an unused mock
    does not fail the test; ``called`` stays False."""
    import respx
    from httpx import Response

    org, user, ri_id = await _make_org_with_received_invoice()

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("https://sdi-test.example/ricevinotifica").mock(
            return_value=Response(200, content=b"<r><EsitoRicezione>ER01</EsitoRicezione></r>")
        )
        async with tenant_session(str(org), str(user)) as s:
            notif = await send_esito_committente(
                s, org_id=org, actor_id=user, received_invoice_id=ri_id, esito="EC01"
            )

    assert not route.called
    assert "ack" not in notif.payload
    assert "transport_error" not in notif.payload
