"""SdI passive cycle: ingest a FatturaElettronica that SdI delivers to our
accredited channel (we are the cessionario / committente).

The endpoint that receives this lives in ``flow_sdi_inbound.app``: it routes
between active-cycle notifications (RC/MC/NS/AT, see services.sdi_inbound)
and passive-cycle invoices (this module) based on the root element name.

Wire shape SdI uses (SDICoop ``RicezioneFatture.RiceviFatture``): a SOAP
envelope whose body carries ``IdentificativoSdI`` + ``NomeFile`` + ``File``
(base64 of the FatturaElettronica). We unwrap once, decode the file, parse
the inner ``FatturaElettronica`` to extract the recipient
``CodiceDestinatario`` and the sender identity, and store the raw bytes
verbatim. Cross-org resolution mirrors ``sdi_resolve_invoice_org`` (the
0074 owner-bypass pattern): a SECURITY DEFINER ``sdi_resolve_recipient_org``
returns the org_id from the IssuerProfile bearing the codice; the actual
insert then runs under a normal tenant_session.

Status is set to ``new``: a follow-up worker pipeline will classify,
notify the user and, for PA, build EsitoCommittente. ADR-0011 v1 keeps
that pipeline deferred; this scaffold is only here so that WSR01 passes
on the AdE interoperability plan and a real passive delivery does not
get bounced as MC.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass

import lxml.etree as ET
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from flow_core.db import admin_session, tenant_session
from flow_core.models.sdi_notification import ReceivedInvoiceNotification
from flow_core.models.sdi_received import ReceivedInvoice

# Nil UUID used as the actor_id for tenant_session: the passive inbound is a
# system writer, no human actor; mirrors services.sdi_inbound conventions.
_SYSTEM_USER = "00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True)
class PassiveDelivery:
    """Structured view of a SdI delivery wrapper (the SOAP body)."""

    identificativo_sdi: str
    file_name: str
    fattura_xml: bytes


@dataclass(frozen=True)
class FatturaHeader:
    """The header fields we need from the FatturaElettronica to route +
    persist the delivery. Everything else stays in the raw XML; this is a
    deliberately narrow projection."""

    transmission_format: str
    sender_country_code: str
    sender_vat_number: str
    sender_legal_name: str | None
    sdi_code: str


def _find_local(root: ET._Element, localname: str) -> ET._Element | None:
    """Namespace-agnostic descendant lookup by local name. Same helper as
    services.sdi_inbound; kept duplicated to avoid a cross-import."""
    for el in root.iter():
        if isinstance(el.tag, str) and ET.QName(el).localname == localname:
            return el
    return None


def _find_local_text(root: ET._Element, localname: str) -> str | None:
    el = _find_local(root, localname)
    if el is None or el.text is None:
        return None
    return el.text.strip() or None


def unwrap_passive_delivery(raw: bytes) -> PassiveDelivery:
    """Parse SdI's SOAP wrapper for a RiceviFatture push and return the
    inner FatturaElettronica + the channel metadata. Raises ValueError if
    the shape is not recognized (the caller turns it into a 400)."""
    try:
        root = ET.fromstring(raw)
    except ET.XMLSyntaxError as exc:
        raise ValueError(f"not well-formed: {exc}") from exc
    ident = _find_local_text(root, "IdentificativoSdI")
    first_name = _find_local_text(root, "NomeFile")
    file_el = _find_local(root, "File")
    if not ident or not first_name or file_el is None or not file_el.text:
        raise ValueError("not a SdI RiceviFatture delivery (missing IdSdI/NomeFile/File)")
    try:
        inner = base64.b64decode(file_el.text)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"File element is not valid base64: {exc}") from exc
    return PassiveDelivery(identificativo_sdi=ident, file_name=first_name, fattura_xml=inner)


def parse_fattura_header(fattura_xml: bytes) -> FatturaHeader:
    """Extract the routing-relevant fields from a FatturaElettronica.
    Raises ValueError when the document is not a recognizable FatturaPA
    (missing root element or any mandatory field)."""
    try:
        doc = ET.fromstring(fattura_xml)
    except ET.XMLSyntaxError as exc:
        raise ValueError(f"FatturaElettronica not well-formed: {exc}") from exc
    if ET.QName(doc).localname != "FatturaElettronica":
        raise ValueError(
            f"root element is {ET.QName(doc).localname!r}, expected FatturaElettronica"
        )
    dt = _find_local(doc, "DatiTrasmissione")
    if dt is None:
        raise ValueError("FatturaElettronica missing DatiTrasmissione")
    formato = _find_local_text(dt, "FormatoTrasmissione") or "FPR12"
    codice = _find_local_text(dt, "CodiceDestinatario")
    if not codice:
        raise ValueError("FatturaElettronica missing CodiceDestinatario")
    cedente = _find_local(doc, "CedentePrestatore")
    if cedente is None:
        raise ValueError("FatturaElettronica missing CedentePrestatore")
    sender_id = _find_local(cedente, "IdFiscaleIVA")
    if sender_id is None:
        raise ValueError("FatturaElettronica missing CedentePrestatore/IdFiscaleIVA")
    country_code = _find_local_text(sender_id, "IdPaese")
    sender_codice = _find_local_text(sender_id, "IdCodice")
    if not country_code or not sender_codice:
        raise ValueError("FatturaElettronica missing CedentePrestatore IdPaese/IdCodice")
    # Denominazione is optional in the standard (persona fisica uses
    # Nome+Cognome); we record whichever is present for triage purposes.
    den = _find_local_text(cedente, "Denominazione")
    if den is None:
        first_name = _find_local_text(cedente, "Nome") or ""
        cogn = _find_local_text(cedente, "Cognome") or ""
        joined = f"{first_name} {cogn}".strip()
        den = joined or None
    return FatturaHeader(
        transmission_format=formato,
        sender_country_code=country_code,
        sender_vat_number=sender_codice,
        sender_legal_name=den,
        sdi_code=codice,
    )


async def _resolve_recipient_org(codice: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Cross-org correlation by recipient CodiceDestinatario via the
    SECURITY DEFINER resolver (migration 0102). Returns (org_id,
    issuer_profile_id) or None when no IssuerProfile owns that codice.

    admin_session has RLS active but no tenant GUC, so a direct SELECT
    on issuer_profiles sees zero rows; we call the SECURITY DEFINER
    function instead (same pattern as services.sdi_inbound._resolve_org
    and migration 0074)."""
    async with admin_session() as s:
        row = (
            await s.execute(
                text("SELECT org_id, issuer_profile_id FROM sdi_resolve_recipient_org(:c)"),
                {"c": codice},
            )
        ).first()
    if row is None:
        return None
    return uuid.UUID(str(row[0])), uuid.UUID(str(row[1]))


async def ingest_passive_invoice(raw: bytes) -> ReceivedInvoice | None:
    """Unwrap, parse, resolve, store. Returns the persisted row, or None
    when no IssuerProfile owns the codice destinatario (the delivery is
    logged as orphan and the endpoint still answers 200; SdI does not
    retry on a 200, so the operator can backfill the codice and a new
    delivery will succeed). A duplicate IdentificativoSdI (SdI retry of
    the same delivery) is idempotent via the unique index -> the
    existing row is returned untouched."""
    delivery = unwrap_passive_delivery(raw)
    header = parse_fattura_header(delivery.fattura_xml)
    resolved = await _resolve_recipient_org(header.sdi_code)
    if resolved is None:
        return None
    org_id, issuer_id = resolved
    async with tenant_session(str(org_id), _SYSTEM_USER) as s:
        # ON CONFLICT (identificativo_sdi) DO NOTHING -> SdI retry safe.
        stmt = (
            pg_insert(ReceivedInvoice)
            .values(
                org_id=org_id,
                issuer_profile_id=issuer_id,
                identificativo_sdi=delivery.identificativo_sdi,
                file_name=delivery.file_name,
                transmission_format=header.transmission_format,
                sender_country_code=header.sender_country_code,
                sender_vat_number=header.sender_vat_number,
                sender_legal_name=header.sender_legal_name,
                sdi_code=header.sdi_code,
                raw_xml=delivery.fattura_xml,
            )
            .on_conflict_do_nothing(index_elements=["identificativo_sdi"])
            .returning(ReceivedInvoice.id)
        )
        result = await s.execute(stmt)
        inserted_id = result.scalar()
    if inserted_id is None:
        # Duplicate delivery: load the existing row for the return value.
        async with tenant_session(str(org_id), _SYSTEM_USER) as s:
            from sqlalchemy import select

            existing = (
                await s.execute(
                    select(ReceivedInvoice).where(
                        ReceivedInvoice.identificativo_sdi == delivery.identificativo_sdi
                    )
                )
            ).scalar_one()
            return existing
    # Re-load to return the full ORM instance under a fresh session.
    async with tenant_session(str(org_id), _SYSTEM_USER) as s:
        from sqlalchemy import select

        row = (
            await s.execute(select(ReceivedInvoice).where(ReceivedInvoice.id == inserted_id))
        ).scalar_one()
        return row


def is_passive_delivery(raw: bytes) -> bool:
    """Quick router predicate: True iff the payload is a passive
    FatturaElettronica delivery -- either a bare ``FatturaElettronica`` root
    or a SdI ``RiceviFatture`` SOAP wrapper whose base64 ``File`` decodes to
    one. False for *every* notification root (RC/MC/NS/AT/NE/DT/MT/SE/EC);
    they all carry a ``NomeFile`` too, so a structural test on its presence
    alone is not enough. Returns False on a malformed payload (the caller
    dispatches it through the active parser, which surfaces it as 400)."""
    try:
        root = ET.fromstring(raw)
    except ET.XMLSyntaxError:
        return False
    if ET.QName(root).localname == "FatturaElettronica":
        return True
    # SOAP wrapper case: the inner File must base64-decode to a
    # FatturaElettronica. Notifications never embed a fattura.
    file_el = _find_local(root, "File")
    if file_el is None or not file_el.text:
        return False
    try:
        inner = base64.b64decode(file_el.text)
        inner_root = ET.fromstring(inner)
    except (ValueError, TypeError, ET.XMLSyntaxError):
        return False
    return bool(ET.QName(inner_root).localname == "FatturaElettronica")


def is_receiver_notification(raw: bytes) -> bool:
    """True iff the payload is a receiver-cycle notification root our
    receiver pipeline handles directly (MT / SE). DT lives on this cycle too
    but is dual-direction: the active dispatcher tries Invoice first, then
    falls back to ReceivedInvoice, so DT routes through the active path."""
    try:
        root = ET.fromstring(raw)
    except ET.XMLSyntaxError:
        return False
    return ET.QName(root).localname in {"MetadatiInvioFile", "ScartoEsitoCommittente"}


# Receiver-cycle notification root -> kind code stored on
# received_invoice_notifications. Mirrors _ROOT_OUTCOME in sdi_inbound for
# the active cycle.
_RECEIVER_ROOT_KIND: dict[str, str] = {
    "MetadatiInvioFile": "MT",
    "ScartoEsitoCommittente": "SE",
}


async def _resolve_received_invoice_org(identificativo: str) -> uuid.UUID | None:
    """Resolve org_id for a received_invoice by IdentificativoSdI. Uses the
    SECURITY DEFINER ``sdi_resolve_received_invoice_org`` (migration 0002),
    same owner-bypass pattern as the transmitter resolver."""
    async with admin_session() as s:
        val = (
            await s.execute(
                text("SELECT sdi_resolve_received_invoice_org(:ident)"),
                {"ident": identificativo},
            )
        ).scalar()
    if val is None:
        return None
    return uuid.UUID(str(val))


async def ingest_receiver_dt(parsed) -> ReceivedInvoice | None:  # type: ignore[no-untyped-def]
    """Apply a receiver-side ``NotificaDecorrenzaTermini``: the 15-day
    window for us to send EsitoCommittente expired, so the invoice is
    deemed accepted. The XSD validation + parse already ran upstream
    (sdi_inbound.parse_notification); here we only carry the structured
    fields and update buyer_verdict + dt_received_at on the
    received invoice + append the audit row."""
    # Local imports to avoid an import cycle with sdi_inbound.
    import datetime as _dt

    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.future import select

    from flow_core.models.invoice import BuyerVerdict

    ident = parsed.identificativo_sdi
    org_id = await _resolve_received_invoice_org(ident)
    if org_id is None:
        return None
    async with tenant_session(str(org_id), _SYSTEM_USER) as s:
        ri = (
            await s.execute(
                select(ReceivedInvoice).where(ReceivedInvoice.identificativo_sdi == ident)
            )
        ).scalar_one_or_none()
        if ri is None:
            return None
        now = _dt.datetime.now(tz=_dt.UTC)
        ri.dt_received_at = now
        if ri.buyer_verdict is BuyerVerdict.none:
            ri.buyer_verdict = BuyerVerdict.deemed_accepted
            ri.buyer_verdict_at = now
        ri.version += 1

        notif = ReceivedInvoiceNotification(
            org_id=org_id,
            received_invoice_id=ri.id,
            kind="DT",
            direction="in",
            file_name=parsed.file_name,
            message_id=parsed.message_id,
            raw_xml=parsed.raw_xml,
            payload={"outcome": "DT"},
        )
        s.add(notif)
        try:
            await s.flush()
        except IntegrityError:
            await s.rollback()
        return ri


async def ingest_receiver_notification(raw: bytes) -> ReceivedInvoice | None:
    """Apply an MT or SE notification: write the audit row on
    ``received_invoice_notifications`` and (for SE) record that the
    committente outcome we sent was rejected by SdI. Returns the received
    invoice, or None if no match (SdI may retry; never 500).

    Validation gates entry, namespace-agnostic parse extracts the fields, the
    SECURITY DEFINER resolver finds the right org, and the audit insert is
    idempotent on the dedupe unique index for SdI retries."""
    # Local imports to keep the validator dependency lazy and avoid cycles.
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.future import select

    from flow_core.services.sdi_notification_xsd import validate_sdi_notification

    errors = validate_sdi_notification(raw)
    if errors:
        raise ValueError(
            "SdI receiver notification fails XSD MessaggiTypes_v1.1: " + "; ".join(errors[:3])
        )
    root = ET.fromstring(raw)
    localname = ET.QName(root).localname
    if localname not in _RECEIVER_ROOT_KIND:
        raise ValueError(f"not a receiver-cycle notification: {localname!r}")
    ident = _find_local_text(root, "IdentificativoSdI")
    if not ident:
        raise ValueError("receiver notification has empty IdentificativoSdI")
    kind = _RECEIVER_ROOT_KIND[localname]
    file_name = _find_local_text(root, "NomeFile")
    message_id = _find_local_text(root, "MessageId")

    org_id = await _resolve_received_invoice_org(ident)
    if org_id is None:
        return None

    async with tenant_session(str(org_id), _SYSTEM_USER) as s:
        ri = (
            await s.execute(
                select(ReceivedInvoice).where(ReceivedInvoice.identificativo_sdi == ident)
            )
        ).scalar_one_or_none()
        if ri is None:
            # Resolver said this org has it, but RLS hid it -- shouldn't
            # happen, but treat as a no-op so SdI doesn't retry indefinitely.
            return None
        notif = ReceivedInvoiceNotification(
            org_id=org_id,
            received_invoice_id=ri.id,
            kind=kind,
            direction="in",
            file_name=file_name,
            message_id=message_id,
            raw_xml=raw,
            payload={"outcome": kind},
        )
        s.add(notif)
        try:
            await s.flush()
        except IntegrityError:
            # Duplicate (same kind + message_id): SdI retry, swallow.
            await s.rollback()
        return ri
