"""SdI inbound notification ingest (docs/adr/0011, FR-9 / F7b + v2).

SdI pushes active-cycle outcome notifications (RC/MC/NS/AT/NE/DT) to the
trasmittente's always-on endpoint. Since one accredited channel serves all
tenants, each notification is correlated to the right tenant by
``IdentificativoSdI`` -- a cross-org lookup with no tenant context. That
lookup goes through the SECURITY DEFINER ``sdi_resolve_invoice_org`` (the
0068 owner-bypass pattern); the status update then runs through a normal
tenant_session so the mutation stays RLS-scoped to the resolved org.

Payload is XSD-validated against the official ``MessaggiTypes_v1.1`` schema
before fields are extracted (see ``sdi_notification_xsd``); a malformed or
non-active payload raises ``ValueError`` rather than falling through to a
lax XPath read. The validator is signature-relaxed (XAdES verification is a
separate concern, deferred per ADR-0011). Field extraction itself stays
namespace-agnostic and handles both bare notification XML and the SOAP /
base64-``File`` delivery wrapper.

NE (NotificaEsito) and DT (NotificaDecorrenzaTermini) extend the v1 set
(ADR-0011 v2): NE carries the buyer's EsitoCommittente (accept/reject), DT
marks the 15-day window expired (deemed acceptance). The full audit trail
of every notification, including the raw signed XML, is appended to
``invoice_notifications`` for compliance.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass

import lxml.etree as ET
from sqlalchemy import text

from flow_core.db import admin_session, tenant_session
from flow_core.models.invoice import Invoice
from flow_core.services import invoice as invoice_svc
from flow_core.services.sdi_notification_xsd import (
    ACTIVE_CYCLE_ROOTS,
    DUAL_CYCLE_ROOTS,
    validate_sdi_notification,
)

# Root element -> active-cycle outcome code (the Invoice ``sdi_status`` value).
_ROOT_OUTCOME: dict[str, str] = {
    "RicevutaConsegna": "RC",
    "NotificaMancataConsegna": "MC",
    "NotificaScarto": "NS",
    "AttestazioneTrasmissioneFattura": "AT",
    "NotificaEsito": "NE",
    "NotificaDecorrenzaTermini": "DT",
    # B2B/B2C "privati" roots: a scarto is RicevutaScarto (NS), an undeliverable
    # is RicevutaImpossibilitaRecapito (MC). RicevutaConsegna (RC) is shared.
    "RicevutaScarto": "NS",
    "RicevutaImpossibilitaRecapito": "MC",
}
# Notifications the active-cycle parser handles. DT is dual-cycle: the same
# root is delivered to transmitter and receiver; here we accept it and treat
# it as transmitter-side (the receiver path is handled by ``sdi_passive``).
_ACTIVE_ROOTS: frozenset[str] = ACTIVE_CYCLE_ROOTS | DUAL_CYCLE_ROOTS

# Nil UUID for the tenant_session user GUC: the invoice RLS policy keys on the
# org only; the system actor is recorded as None in the audit trail.
_SYSTEM_USER = "00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True)
class ParsedNotification:
    """Everything the dispatcher needs to apply a notification.

    ``outcome`` is the active-cycle code (RC/MC/NS/AT/NE/DT). ``esito`` is the
    buyer's EC code (EC01 accepted / EC02 rejected) extracted from a NE; for
    every other kind it is None. ``raw_xml`` is the unwrapped payload, which
    is what the audit log stores -- not the SOAP envelope or the base64 blob.
    """

    outcome: str
    identificativo_sdi: str
    message_id: str | None
    nome_file: str | None
    esito: str | None
    raw_xml: bytes


def _find_local(root: ET._Element, localname: str) -> ET._Element | None:
    for el in root.iter():
        if isinstance(el.tag, str) and ET.QName(el).localname == localname:
            return el
    return None


def _maybe_unwrap(text_val: str) -> ET._Element | None:
    """A SOAP/fileSdI delivery carries the notification as base64 in a
    ``File`` element. Decode + parse it, or None if it is not that."""
    try:
        return ET.fromstring(base64.b64decode(text_val))
    except (ValueError, ET.XMLSyntaxError):
        return None


def _text(el: ET._Element | None) -> str | None:
    if el is None or not el.text:
        return None
    val = el.text.strip()
    return val or None


def parse_notification(raw: bytes) -> ParsedNotification:
    """Validate + extract every field the dispatcher needs from an active-cycle
    SdI notification. Unwraps a base64 ``File`` if present. Raises
    ``ValueError`` if XSD validation fails or the root is not an active
    notification (receiver-cycle roots are dispatched upstream)."""
    root = ET.fromstring(raw)
    wrapped = _find_local(root, "File")
    if wrapped is not None and wrapped.text:
        unwrapped = _maybe_unwrap(wrapped.text)
        if unwrapped is not None:
            root = unwrapped
    unwrapped_bytes = ET.tostring(root)
    xsd_errors = validate_sdi_notification(unwrapped_bytes)
    if xsd_errors:
        raise ValueError(
            "SdI notification fails XSD MessaggiTypes_v1.1: " + "; ".join(xsd_errors[:3])
        )
    localname = ET.QName(root).localname
    if localname not in _ACTIVE_ROOTS:
        raise ValueError(
            f"SdI notification '{localname}' is a receiver-cycle root and must be dispatched "
            "by sdi_passive, not the active-cycle parser"
        )
    outcome = _ROOT_OUTCOME[localname]
    ident = _text(_find_local(root, "IdentificativoSdI"))
    if not ident:
        raise ValueError("SdI notification has empty IdentificativoSdI")
    # ``MessageId`` (max 14) is the SdI side of the dedupe key on
    # invoice_notifications. NE/DT carry a NomeFile too; RC/MC/NS/AT always.
    esito: str | None = None
    if localname == "NotificaEsito":
        # EsitoCommittente nests a second IdentificativoSdI for the original
        # invoice; we already have the outer one. We only need the verdict.
        ec = _find_local(root, "EsitoCommittente")
        if ec is not None:
            esito = _text(_find_local(ec, "Esito"))
    return ParsedNotification(
        outcome=outcome,
        identificativo_sdi=ident,
        message_id=_text(_find_local(root, "MessageId")),
        nome_file=_text(_find_local(root, "NomeFile")),
        esito=esito,
        raw_xml=unwrapped_bytes,
    )


async def _resolve_org(identificativo: str) -> uuid.UUID | None:
    """Cross-org correlation by IdentificativoSdI via the SECURITY DEFINER
    resolver (bypasses RLS for this one lookup only; migration 0074)."""
    async with admin_session() as s:
        val = (
            await s.execute(
                text("SELECT sdi_resolve_invoice_org(:ident)"), {"ident": identificativo}
            )
        ).scalar()
    if val is None:
        return None
    return uuid.UUID(str(val))


async def ingest_notification(raw: bytes) -> Invoice | None:
    """Parse + apply an SdI notification. Returns the updated invoice, or None
    if no invoice matches the IdentificativoSdI (SdI may retry; do not 500).

    DT is dual-cycle: SdI sends the same NotificaDecorrenzaTermini both to
    the transmitter (15 days without buyer EC) and to the receiver (15 days
    without our outbound EC). We resolve transmitter first; if that misses,
    a DT also probes the receiver side via the passive resolver."""
    parsed = parse_notification(raw)
    org_id = await _resolve_org(parsed.identificativo_sdi)
    if org_id is not None:
        async with tenant_session(str(org_id), _SYSTEM_USER) as s:
            return await invoice_svc.ingest_active_notification(
                s,
                org_id=org_id,
                actor_id=None,
                parsed=parsed,
            )
    if parsed.outcome == "DT":
        # Receiver-side fallback: a DT that does not match any transmitted
        # invoice may belong to one we *received*.
        from flow_core.services.sdi_passive import ingest_receiver_dt

        await ingest_receiver_dt(parsed)
    return None
