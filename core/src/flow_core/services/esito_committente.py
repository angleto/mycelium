"""EsitoCommittente outbound (ADR-0011 v1.1): we (as cessionario) build,
sign and persist the NotificaEsitoCommittente that tells SdI whether we
accept or reject a received invoice.

The wire shape is governed by ``MessaggiTypes_v1.1.xsd``:
``NotificaEsitoCommittente_Type`` carries IdentificativoSdI, optional
RiferimentoFattura, Esito (EC01 accepted | EC02 rejected), optional
Descrizione, optional MessageIdCommittente, and an optional XMLDSig
``ds:Signature`` enveloped in the root. Real SdI submissions require the
signature; this module produces an enveloped XMLDSig with ``signxml``.

What stays out of scope here:
- The actual SOAP/HTTPS submission to SdI. The accreditation step that
  authenticates the channel and the trasmissione endpoint are exercised
  separately (F7c). This module returns the signed XML and the persisted
  audit row; a thin transport adapter (or the operator) ships the bytes.
- Qualified signature (CAdES/XAdES with EU-trusted CA): post-v1.1, the
  XMLDSig is sufficient for the buyer-side EC submission to AdE.
"""

from __future__ import annotations

import datetime
import uuid

import lxml.etree as ET
from cryptography.hazmat.primitives import serialization
from signxml import DigestAlgorithm, SignatureConstructionMethod, SignatureMethod, XMLSigner
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.config import get_settings
from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.sdi_notification import ReceivedInvoiceNotification
from flow_core.models.sdi_received import CommittenteVerdict, ReceivedInvoice
from flow_core.services import audit
from flow_core.services.sdi_notification_xsd import NS_MESSAGGI, validate_sdi_notification

# EC esito codes -> committente verdict mapping. The XSD restricts the
# Esito element to exactly these two values (EC01 ACCETTAZIONE, EC02
# RIFIUTO); see MessaggiTypes_v1.1.xsd.
_ESITO_VERDICT: dict[str, CommittenteVerdict] = {
    "EC01": CommittenteVerdict.accepted,
    "EC02": CommittenteVerdict.rejected,
}


def _load_signing_material() -> tuple[bytes, bytes]:
    """Return (key_pem, cert_pem) from settings. Raises ``DomainError`` if
    EC outbound was not provisioned, so the operator gets a precise error
    instead of a cryptic libxml failure deep inside signxml."""
    s = get_settings()
    if not s.sdi_ec_signing_key_pem or not s.sdi_ec_signing_cert_pem:
        raise DomainError(
            MessageCode.DOMAIN_ERROR,
            detail=(
                "EC outbound not provisioned: set sdi_ec_signing_key_pem / "
                "sdi_ec_signing_cert_pem"
            ),
        )
    return s.sdi_ec_signing_key_pem.encode(), s.sdi_ec_signing_cert_pem.encode()


def build_esito_committente_xml(
    *,
    identificativo_sdi: str,
    esito: str,
    descrizione: str | None = None,
    message_id_committente: str | None = None,
) -> bytes:
    """Build the unsigned ``NotificaEsitoCommittente`` element and sign it
    with an XMLDSig enveloped signature. Returns the signed XML bytes ready
    for SdI submission.

    The element layout mirrors ``MessaggiTypes_v1.1`` exactly (root in the
    messaggi namespace, children unqualified). XSD validation of the signed
    bytes is run as a self-check; a configuration that produces an invalid
    payload fails here instead of being silently shipped to SdI.
    """
    if esito not in _ESITO_VERDICT:
        raise DomainError(
            MessageCode.DOMAIN_ERROR, detail=f"esito must be EC01 or EC02, got {esito!r}"
        )

    root = ET.Element(
        f"{{{NS_MESSAGGI}}}NotificaEsitoCommittente",
        nsmap={"m": NS_MESSAGGI},
        attrib={"versione": "1.0"},
    )
    ET.SubElement(root, "IdentificativoSdI").text = identificativo_sdi
    ET.SubElement(root, "Esito").text = esito
    if descrizione:
        ET.SubElement(root, "Descrizione").text = descrizione
    if message_id_committente:
        ET.SubElement(root, "MessageIdCommittente").text = message_id_committente

    key_pem, cert_pem = _load_signing_material()
    key = serialization.load_pem_private_key(key_pem, password=None)
    signer = XMLSigner(
        method=SignatureConstructionMethod.enveloped,
        signature_algorithm=SignatureMethod.RSA_SHA256,
        digest_algorithm=DigestAlgorithm.SHA256,
    )
    signed = signer.sign(root, key=key, cert=cert_pem)

    payload = ET.tostring(signed, xml_declaration=True, encoding="UTF-8")
    # Self-check: a misconfigured signer is a deploy bug; surface it here
    # rather than at the SdI esito.
    errors = validate_sdi_notification(payload)
    if errors:
        raise DomainError(
            MessageCode.DOMAIN_ERROR,
            detail="generated EC fails XSD: " + "; ".join(errors[:3]),
        )
    return payload


async def send_esito_committente(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    received_invoice_id: uuid.UUID,
    esito: str,
    descrizione: str | None = None,
) -> ReceivedInvoiceNotification:
    """Build the signed EC, persist it on ``received_invoice_notifications``
    with ``direction='out'`` and update the denormalized committente verdict
    on the parent received invoice. The actual SdI submission is decoupled
    from this call (handled by a transport adapter, F7c) -- this is the
    durable persistence step.
    """
    ri = (
        await session.execute(
            select(ReceivedInvoice).where(ReceivedInvoice.id == received_invoice_id)
        )
    ).scalar_one_or_none()
    if ri is None:
        raise NotFoundError(MessageCode.DOMAIN_ERROR, detail="received_invoice not found")

    message_id = uuid.uuid4().hex[:14]
    signed_xml = build_esito_committente_xml(
        identificativo_sdi=ri.identificativo_sdi,
        esito=esito,
        descrizione=descrizione,
        message_id_committente=message_id,
    )

    notif = ReceivedInvoiceNotification(
        org_id=org_id,
        received_invoice_id=ri.id,
        kind="EC",
        direction="out",
        nome_file=None,
        message_id=message_id,
        raw_xml=signed_xml,
        payload={"esito": esito, "descrizione": descrizione},
    )
    session.add(notif)

    verdict = _ESITO_VERDICT[esito]
    ri.committente_verdict = verdict
    ri.committente_verdict_at = datetime.datetime.now(tz=datetime.UTC)
    ri.version += 1

    try:
        await session.flush()
    except IntegrityError as exc:
        # Same EC message_id already sent for this received invoice: that is
        # a caller bug (re-issuing the same id), surface it cleanly.
        await session.rollback()
        raise DomainError(
            MessageCode.DOMAIN_ERROR,
            detail="EC with this message_id already exists for this received invoice",
        ) from exc

    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="received_invoice",
        entity_id=ri.id,
        action="sdi_ec_sent",
        diff={"esito": esito, "message_id": message_id},
    )
    return notif
