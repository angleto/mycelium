"""Received-invoices API: POST /received-invoices/{id}/esito-committente.

Drives the EsitoCommittente outbound endpoint end-to-end through the live
FastAPI app: signup -> issuer with codice destinatario -> a
ReceivedInvoice persisted by the passive ingest -> POST EC -> server-side
build+sign+persist via services.esito_committente. Asserts the HTTP
contract (shape, 200, idempotency-style error) and that the denormalized
verdict on received_invoices moves to ``accepted``/``rejected``.
"""

from __future__ import annotations

import base64
import datetime
import os
import uuid
from collections.abc import Iterator

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from httpx import ASGITransport, AsyncClient

from flow_api.main import app
from flow_core.config import get_settings
from flow_core.db import tenant_session
from flow_core.models.sdi_received import BuyerVerdict, ReceivedInvoice
from flow_core.services.sdi_passive import ingest_passive_invoice


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _generate_signing_material() -> tuple[str, str]:
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
    return (
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        cert.public_bytes(serialization.Encoding.PEM).decode(),
    )


@pytest.fixture
def _ec_signing() -> Iterator[None]:
    key_pem, cert_pem = _generate_signing_material()
    os.environ["FLOW_SDI_EC_SIGNING_KEY_PEM"] = key_pem
    os.environ["FLOW_SDI_EC_SIGNING_CERT_PEM"] = cert_pem
    get_settings.cache_clear()
    try:
        yield
    finally:
        del os.environ["FLOW_SDI_EC_SIGNING_KEY_PEM"]
        del os.environ["FLOW_SDI_EC_SIGNING_CERT_PEM"]
        get_settings.cache_clear()


_NS = "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2"
_FATTURA_TPL = (
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
    "<Sede><Indirizzo>Via Cedente 1</Indirizzo>"
    "<CAP>20100</CAP><Comune>Milano</Comune><Nazione>IT</Nazione></Sede>"
    "</CedentePrestatore>"
    "</FatturaElettronicaHeader>"
    "</p:FatturaElettronica>"
)


def _wrap_riceve_fatture(fattura: bytes, ident: str, first_name: str) -> bytes:
    b64 = base64.b64encode(fattura).decode("ascii")
    return (
        f'<?xml version="1.0"?><RiceviFattureRequest>'
        f"<IdentificativoSdI>{ident}</IdentificativoSdI>"
        f"<NomeFile>{first_name}</NomeFile>"
        f"<File>{b64}</File>"
        f"</RiceviFattureRequest>"
    ).encode()


async def test_post_esito_committente_endpoint(_ec_signing: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        signed = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "EC"},
            )
        ).json()
        h = {
            "Authorization": f"Bearer {signed['token']}",
            "X-Workspace-Id": signed["workspace_id"],
            "X-Workspace-Role": "owner",
        }
        # Set up issuer with a codice destinatario; the API has no
        # "create received invoice" route (SdI is the only writer), so we
        # inject it via the passive ingest below.
        codice = uuid.uuid4().hex[:7].upper()
        prof = (
            await c.post(
                "/issuer-profiles",
                headers=h,
                json={
                    "label": "R",
                    "legal_name": "Recipient SRL",
                    "vat_number": "13438810015",
                    "address": "Via Test 1",
                    "postal_code": "10100",
                    "city": "Torino",
                },
            )
        ).json()
        # Promote the codice; no dedicated endpoint yet, so update in DB.
        org_id = uuid.UUID(signed["workspace_id"])
        user_id = uuid.UUID(signed["user_id"])
        from sqlalchemy import select

        from flow_core.models.invoice import IssuerProfile

        async with tenant_session(str(org_id), str(user_id)) as s:
            ip = (
                await s.execute(
                    select(IssuerProfile).where(IssuerProfile.id == uuid.UUID(prof["id"]))
                )
            ).scalar_one()
            ip.sdi_code = codice
            await s.flush()

        # Deliver a passive invoice as SdI would.
        ident = str(int(uuid.uuid4().hex[:9], 16) % 10**12)
        await ingest_passive_invoice(
            _wrap_riceve_fatture(
                _FATTURA_TPL.format(codice=codice).encode(),
                ident=ident,
                first_name=f"IT09876543210_{uuid.uuid4().hex[:5].upper()}.xml",
            )
        )
        async with tenant_session(str(org_id), str(user_id)) as s:
            ri = (
                await s.execute(
                    select(ReceivedInvoice).where(ReceivedInvoice.identificativo_sdi == ident)
                )
            ).scalar_one()
        ri_id = str(ri.id)

        # Happy path: EC01 accepted.
        r = await c.post(
            f"/received-invoices/{ri_id}/esito-committente",
            headers=h,
            json={"esito": "EC01"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["received_invoice_id"] == ri_id
        assert body["esito"] == "EC01"
        assert body["message_id"]

        # Denormalized verdict updated.
        async with tenant_session(str(org_id), str(user_id)) as s:
            updated = (
                await s.execute(select(ReceivedInvoice).where(ReceivedInvoice.id == ri.id))
            ).scalar_one()
        assert updated.buyer_verdict is BuyerVerdict.accepted

        # Bad esito: schema rejection (422 from FastAPI validation).
        r_bad = await c.post(
            f"/received-invoices/{ri_id}/esito-committente",
            headers=h,
            json={"esito": "EC99"},
        )
        assert r_bad.status_code == 422

        # Unknown received_invoice_id: domain 404.
        r_404 = await c.post(
            f"/received-invoices/{uuid.uuid4()}/esito-committente",
            headers=h,
            json={"esito": "EC02", "descrizione": "merce non conforme"},
        )
        assert r_404.status_code == 404
