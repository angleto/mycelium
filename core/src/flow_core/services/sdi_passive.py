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
from flow_core.models.sdi_received import ReceivedInvoice

# Nil UUID used as the actor_id for tenant_session: the passive inbound is a
# system writer, no human actor; mirrors services.sdi_inbound conventions.
_SYSTEM_USER = "00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True)
class PassiveDelivery:
    """Structured view of a SdI delivery wrapper (the SOAP body)."""

    identificativo_sdi: str
    nome_file: str
    fattura_xml: bytes


@dataclass(frozen=True)
class FatturaHeader:
    """The header fields we need from the FatturaElettronica to route +
    persist the delivery. Everything else stays in the raw XML; this is a
    deliberately narrow projection."""

    formato_trasmissione: str
    sender_id_paese: str
    sender_id_codice: str
    sender_denominazione: str | None
    codice_destinatario: str


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
    nome = _find_local_text(root, "NomeFile")
    file_el = _find_local(root, "File")
    if not ident or not nome or file_el is None or not file_el.text:
        raise ValueError("not a SdI RiceviFatture delivery (missing IdSdI/NomeFile/File)")
    try:
        inner = base64.b64decode(file_el.text)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"File element is not valid base64: {exc}") from exc
    return PassiveDelivery(identificativo_sdi=ident, nome_file=nome, fattura_xml=inner)


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
    paese = _find_local_text(sender_id, "IdPaese")
    sender_codice = _find_local_text(sender_id, "IdCodice")
    if not paese or not sender_codice:
        raise ValueError("FatturaElettronica missing CedentePrestatore IdPaese/IdCodice")
    # Denominazione is optional in the standard (persona fisica uses
    # Nome+Cognome); we record whichever is present for triage purposes.
    den = _find_local_text(cedente, "Denominazione")
    if den is None:
        nome = _find_local_text(cedente, "Nome") or ""
        cogn = _find_local_text(cedente, "Cognome") or ""
        joined = f"{nome} {cogn}".strip()
        den = joined or None
    return FatturaHeader(
        formato_trasmissione=formato,
        sender_id_paese=paese,
        sender_id_codice=sender_codice,
        sender_denominazione=den,
        codice_destinatario=codice,
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
    resolved = await _resolve_recipient_org(header.codice_destinatario)
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
                nome_file=delivery.nome_file,
                formato_trasmissione=header.formato_trasmissione,
                sender_id_paese=header.sender_id_paese,
                sender_id_codice=header.sender_id_codice,
                sender_denominazione=header.sender_denominazione,
                codice_destinatario=header.codice_destinatario,
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
    """Quick router predicate: True iff the payload looks like a SdI
    RiceviFatture wrapper (SOAP envelope or fileSdI carrying a NomeFile
    that points to a FatturaElettronica). Cheap; the deep parse happens
    in ingest_passive_invoice. False for active-cycle notification roots
    (RicevutaConsegna / NotificaScarto / ...). Returns False also on a
    malformed payload (the caller dispatches it through the active
    parser, which will raise ValueError -> 400 from the app)."""
    try:
        root = ET.fromstring(raw)
    except ET.XMLSyntaxError:
        return False
    rootname = ET.QName(root).localname
    # A FatturaElettronica posted unwrapped: also passive.
    if rootname == "FatturaElettronica":
        return True
    # SOAP wrapper carrying base64 File + NomeFile = passive RiceviFatture.
    # Active notifications also use File-base64 wrappers, but they do NOT
    # carry NomeFile -- that field is unique to the passive delivery shape.
    nome = _find_local(root, "NomeFile")
    return nome is not None
