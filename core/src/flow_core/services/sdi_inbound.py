"""SdI inbound notification ingest (docs/adr/0011, FR-9 / F7b).

SdI pushes active-cycle outcome notifications (RC/MC/NS/AT) to the
trasmittente's always-on endpoint. Since one accredited channel serves all
tenants, the notification is correlated to the right tenant by
``IdentificativoSdI`` -- a cross-org lookup with no tenant context. That lookup
goes through the SECURITY DEFINER ``sdi_resolve_invoice_org`` (migration 0074,
the 0068 owner-bypass pattern); the status update then runs through a normal
tenant_session so the mutation stays RLS-scoped to the resolved org.

Parsing is namespace-agnostic and tolerates both a bare notification XML and
the SOAP / base64-``File`` delivery wrapper. The live transport (mutual TLS at
the edge, the exact SOAP esito response) is verified post-accreditation.
"""

from __future__ import annotations

import base64
import uuid

import lxml.etree as ET
from sqlalchemy import text

from flow_core.db import admin_session, tenant_session
from flow_core.models.invoice import Invoice
from flow_core.services import invoice as invoice_svc

# Active-cycle notification root element -> outcome code (ADR-0011 v1).
# NE/DT/EC/SE (PA / passive cycle) are deferred post-v1.
_ROOT_OUTCOME: dict[str, str] = {
    "RicevutaConsegna": "RC",
    "NotificaMancataConsegna": "MC",
    "NotificaScarto": "NS",
    "AttestazioneTrasmissioneFattura": "AT",
    "AttestazioneTrasmissioneFatturaNonRecapitata": "AT",
}
# Nil UUID for the tenant_session user GUC: the invoice RLS policy keys on the
# org only; the system actor is recorded as None in the audit trail.
_SYSTEM_USER = "00000000-0000-0000-0000-000000000000"


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


def parse_notification(raw: bytes) -> tuple[str, str]:
    """Extract (IdentificativoSdI, outcome) from an SdI notification, unwrapping
    a base64 ``File`` if present. Raises ValueError on an unrecognized one."""
    root = ET.fromstring(raw)
    wrapped = _find_local(root, "File")
    if wrapped is not None and wrapped.text:
        unwrapped = _maybe_unwrap(wrapped.text)
        if unwrapped is not None:
            root = unwrapped
    outcome: str | None = None
    for el in root.iter():
        if isinstance(el.tag, str):
            code = _ROOT_OUTCOME.get(ET.QName(el).localname)
            if code is not None:
                outcome = code
                break
    ident_el = _find_local(root, "IdentificativoSdI")
    ident = (ident_el.text or "").strip() if ident_el is not None and ident_el.text else ""
    if not ident or outcome is None:
        raise ValueError("unrecognized SdI notification (no IdentificativoSdI / known outcome)")
    return ident, outcome


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
    if no invoice matches the IdentificativoSdI (SdI may retry; do not 500)."""
    identificativo, outcome = parse_notification(raw)
    org_id = await _resolve_org(identificativo)
    if org_id is None:
        return None
    async with tenant_session(str(org_id), _SYSTEM_USER) as s:
        return await invoice_svc.ingest_receipt(
            s,
            org_id=org_id,
            actor_id=None,
            identificativo_sdi=identificativo,
            outcome=outcome,
        )
