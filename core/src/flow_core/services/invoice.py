"""Italian electronic invoicing service (docs/adr/0009, 0010, 0011,
FR-9).

Legally load-bearing invariants, enforced here:
- only ``draft`` is mutable; after emission the document is
  append-only, correction is a TD04 credit note (ADR-0009);
- the progressive number per (org, series, year) is allocated
  concurrency-safe (counter row, ``FOR UPDATE``) only at
  draft -> transmitted, in the same transaction, never reused;
- the tenant identity is in the FatturaPA payload, not the channel
  (ADR-0011); ``ManualExportChannel`` invoices are out of AdE free
  conservation (ADR-0010), SdI-transited ones become covered.
FatturaPA 1.2 XML is built deterministically and structurally +
arithmetically validated (full XSD validation is a hardening add-on).
"""

from __future__ import annotations

import datetime as dt
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import ConflictError, DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.client_profile import ClientProfile
from flow_core.models.invoice import (
    ConservationAdhesion,
    ConservationStatus,
    DocumentType,
    Invoice,
    InvoiceCounter,
    InvoiceKind,
    InvoiceLine,
    InvoiceState,
    OrgFiscalProfile,
    PaymentStatus,
    SdiStatus,
)
from flow_core.models.membership import Role
from flow_core.sdi_channel import SdiChannel, get_channel
from flow_core.services import audit
from flow_core.services.rbac import require_role

_NS = "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2"


def _q2(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _money(d: Decimal) -> str:
    return f"{_q2(d):.2f}"


@dataclass(frozen=True)
class Totals:
    taxable: Decimal
    vat: Decimal
    total: Decimal


# --- fiscal profile ---


async def get_fiscal_profile(
    session: AsyncSession, *, org_id: uuid.UUID
) -> OrgFiscalProfile | None:
    return (
        await session.execute(select(OrgFiscalProfile).where(OrgFiscalProfile.org_id == org_id))
    ).scalar_one_or_none()


async def upsert_fiscal_profile(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    denominazione: str,
    piva: str | None = None,
    codice_fiscale: str | None = None,
    regime_fiscale: str = "RF01",
    paese: str = "IT",
    indirizzo: str = "",
    cap: str = "",
    comune: str = "",
    provincia: str | None = None,
    nazione: str = "IT",
) -> OrgFiscalProfile:
    await require_role(session, org_id, actor_id, Role.admin)
    prof = await get_fiscal_profile(session, org_id=org_id)
    creating = prof is None
    if prof is None:
        prof = OrgFiscalProfile(org_id=org_id, denominazione=denominazione)
        session.add(prof)
    prof.denominazione = denominazione
    prof.piva = piva
    prof.codice_fiscale = codice_fiscale
    prof.regime_fiscale = regime_fiscale
    prof.paese = paese
    prof.indirizzo = indirizzo
    prof.cap = cap
    prof.comune = comune
    prof.provincia = provincia
    prof.nazione = nazione
    if not creating:
        # version is a flush/server default; only bump an existing row.
        prof.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="fiscal_profile",
        entity_id=None,
        action="upsert",
    )
    return prof


async def set_conservation_adhesion(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    adhesion: str,
) -> OrgFiscalProfile:
    """Track the per-tenant AdE free-conservation adhesion (ADR-0010);
    Flow guides it, it cannot adhere on the tenant's behalf."""
    await require_role(session, org_id, actor_id, Role.admin)
    prof = await get_fiscal_profile(session, org_id=org_id)
    if prof is None:
        raise NotFoundError(MessageCode.FISCAL_PROFILE_REQUIRED, detail="missing")
    prof.conservation_adhesion = ConservationAdhesion(adhesion)
    prof.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="fiscal_profile",
        entity_id=None,
        action="conservation_adhesion",
        diff={"adhesion": adhesion},
    )
    return prof


# --- invoice draft lifecycle ---


async def get_invoice(
    session: AsyncSession, *, org_id: uuid.UUID, invoice_id: uuid.UUID
) -> Invoice:
    inv = (
        await session.execute(select(Invoice).where(Invoice.id == invoice_id))
    ).scalar_one_or_none()
    if inv is None:
        raise NotFoundError(MessageCode.INVOICE_NOT_FOUND)
    return inv


def _require_draft(inv: Invoice) -> None:
    if inv.state is not InvoiceState.draft:
        raise ConflictError(MessageCode.INVOICE_NOT_DRAFT)


async def create_draft(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    client_tag_id: uuid.UUID,
    year: int | None = None,
    series: str = "A",
    causale: str | None = None,
    document_type: DocumentType = DocumentType.TD01,
    kind: InvoiceKind = InvoiceKind.invoice,
    parent_invoice_id: uuid.UUID | None = None,
) -> Invoice:
    await require_role(session, org_id, actor_id, Role.member)
    inv = Invoice(
        org_id=org_id,
        client_tag_id=client_tag_id,
        kind=kind,
        document_type=document_type,
        parent_invoice_id=parent_invoice_id,
        series=series,
        year=year or dt.datetime.now(tz=dt.UTC).year,
        state=InvoiceState.draft,
        causale=causale,
    )
    session.add(inv)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="invoice",
        entity_id=inv.id,
        action="create_draft",
    )
    return inv


async def add_line(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    invoice_id: uuid.UUID,
    description: str,
    unit_price: Decimal,
    quantity: Decimal = Decimal(1),
    vat_rate: Decimal = Decimal(22),
    natura: str | None = None,
) -> InvoiceLine:
    inv = await get_invoice(session, org_id=org_id, invoice_id=invoice_id)
    _require_draft(inv)
    await require_role(session, org_id, actor_id, Role.member)
    existing = list(
        (await session.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice_id)))
        .scalars()
        .all()
    )
    line = InvoiceLine(
        org_id=org_id,
        invoice_id=invoice_id,
        line_no=len(existing) + 1,
        description=description,
        quantity=quantity,
        unit_price=unit_price,
        vat_rate=vat_rate,
        natura=natura,
    )
    session.add(line)
    await session.flush()
    return line


async def list_lines(
    session: AsyncSession, *, org_id: uuid.UUID, invoice_id: uuid.UUID
) -> list[InvoiceLine]:
    return list(
        (
            await session.execute(
                select(InvoiceLine)
                .where(InvoiceLine.invoice_id == invoice_id)
                .order_by(InvoiceLine.line_no)
            )
        )
        .scalars()
        .all()
    )


async def delete_draft(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    invoice_id: uuid.UUID,
) -> None:
    inv = await get_invoice(session, org_id=org_id, invoice_id=invoice_id)
    _require_draft(inv)
    await require_role(session, org_id, actor_id, Role.member)
    await session.execute(delete(Invoice).where(Invoice.id == invoice_id))
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="invoice",
        entity_id=invoice_id,
        action="delete_draft",
    )


def _compute_totals(lines: Sequence[InvoiceLine]) -> Totals:
    taxable = Decimal(0)
    vat = Decimal(0)
    by_rate: dict[Decimal, Decimal] = {}
    for ln in lines:
        line_total = _q2(ln.quantity * ln.unit_price)
        by_rate[ln.vat_rate] = by_rate.get(ln.vat_rate, Decimal(0)) + line_total
    for rate, imponibile in by_rate.items():
        imp = _q2(imponibile)
        imposta = _q2(imp * rate / Decimal(100))
        taxable += imp
        vat += imposta
    return Totals(taxable=_q2(taxable), vat=_q2(vat), total=_q2(taxable + vat))


async def _client(session: AsyncSession, client_tag_id: uuid.UUID) -> ClientProfile:
    cp = (
        await session.execute(select(ClientProfile).where(ClientProfile.tag_id == client_tag_id))
    ).scalar_one_or_none()
    if cp is None:
        raise DomainError(MessageCode.INVOICE_INVALID, detail="client profile missing")
    return cp


def _validate(
    fiscal: OrgFiscalProfile | None,
    client: ClientProfile,
    lines: Sequence[InvoiceLine],
) -> None:
    if fiscal is None:
        raise NotFoundError(MessageCode.FISCAL_PROFILE_REQUIRED, detail="missing")
    missing = [
        f
        for f, v in (
            ("denominazione", fiscal.denominazione),
            ("indirizzo", fiscal.indirizzo),
            ("cap", fiscal.cap),
            ("comune", fiscal.comune),
        )
        if not v
    ]
    if not (fiscal.piva or fiscal.codice_fiscale):
        missing.append("piva|codice_fiscale")
    if missing:
        raise DomainError(MessageCode.FISCAL_PROFILE_REQUIRED, detail=", ".join(missing))
    if not client.ragione_sociale or not (client.id_codice or client.codice_fiscale):
        raise DomainError(MessageCode.INVOICE_INVALID, detail="client fiscal id missing")
    if not (client.codice_destinatario or client.pec):
        raise DomainError(MessageCode.INVOICE_INVALID, detail="client SdI address missing")
    if not lines:
        raise DomainError(MessageCode.INVOICE_INVALID, detail="no lines")


def _sub(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = text
    return el


def _build_xml(
    inv: Invoice,
    fiscal: OrgFiscalProfile,
    client: ClientProfile,
    lines: Sequence[InvoiceLine],
    progressivo: str,
) -> str:
    ET.register_namespace("p", _NS)
    root = ET.Element(f"{{{_NS}}}FatturaElettronica", versione="FPR12")
    header = _sub(root, "FatturaElettronicaHeader")
    dt_ = _sub(header, "DatiTrasmissione")
    idt = _sub(dt_, "IdTrasmittente")
    _sub(idt, "IdPaese", fiscal.paese)
    _sub(idt, "IdCodice", fiscal.piva or fiscal.codice_fiscale or "")
    _sub(dt_, "ProgressivoInvio", progressivo)
    _sub(dt_, "FormatoTrasmissione", "FPR12")
    _sub(dt_, "CodiceDestinatario", client.codice_destinatario or "0000000")
    if not client.codice_destinatario and client.pec:
        cc = _sub(dt_, "ContattiTrasmittente")
        _sub(cc, "Email", client.pec)
    cedente = _sub(header, "CedentePrestatore")
    anag = _sub(cedente, "DatiAnagrafici")
    if fiscal.piva:
        iva = _sub(anag, "IdFiscaleIVA")
        _sub(iva, "IdPaese", fiscal.paese)
        _sub(iva, "IdCodice", fiscal.piva)
    if fiscal.codice_fiscale:
        _sub(anag, "CodiceFiscale", fiscal.codice_fiscale)
    an = _sub(anag, "Anagrafica")
    _sub(an, "Denominazione", fiscal.denominazione)
    _sub(anag, "RegimeFiscale", fiscal.regime_fiscale)
    sede = _sub(cedente, "Sede")
    _sub(sede, "Indirizzo", fiscal.indirizzo)
    _sub(sede, "CAP", fiscal.cap)
    _sub(sede, "Comune", fiscal.comune)
    if fiscal.provincia:
        _sub(sede, "Provincia", fiscal.provincia)
    _sub(sede, "Nazione", fiscal.nazione)
    cess = _sub(header, "CessionarioCommittente")
    canag = _sub(cess, "DatiAnagrafici")
    if client.id_codice:
        civa = _sub(canag, "IdFiscaleIVA")
        _sub(civa, "IdPaese", client.id_paese or "IT")
        _sub(civa, "IdCodice", client.id_codice)
    if client.codice_fiscale:
        _sub(canag, "CodiceFiscale", client.codice_fiscale)
    can = _sub(canag, "Anagrafica")
    _sub(can, "Denominazione", client.ragione_sociale)
    csede = _sub(cess, "Sede")
    _sub(csede, "Indirizzo", client.indirizzo or "")
    _sub(csede, "CAP", client.cap or "")
    _sub(csede, "Comune", client.comune or "")
    if client.provincia:
        _sub(csede, "Provincia", client.provincia)
    _sub(csede, "Nazione", client.nazione or "IT")

    body = _sub(root, "FatturaElettronicaBody")
    dg = _sub(body, "DatiGenerali")
    dgd = _sub(dg, "DatiGeneraliDocumento")
    _sub(dgd, "TipoDocumento", inv.document_type.value)
    _sub(dgd, "Divisa", inv.currency)
    _sub(dgd, "Data", (inv.issued_at or dt.datetime.now(tz=dt.UTC)).date().isoformat())
    _sub(dgd, "Numero", f"{inv.series}{inv.number}")
    _sub(dgd, "ImportoTotaleDocumento", _money(inv.total))
    if inv.causale:
        _sub(dgd, "Causale", inv.causale)
    dbs = _sub(body, "DatiBeniServizi")
    by_rate: dict[Decimal, Decimal] = {}
    for ln in lines:
        dl = _sub(dbs, "DettaglioLinee")
        _sub(dl, "NumeroLinea", str(ln.line_no))
        _sub(dl, "Descrizione", ln.description)
        _sub(dl, "Quantita", f"{ln.quantity:.2f}")
        _sub(dl, "PrezzoUnitario", f"{ln.unit_price:.2f}")
        line_total = _q2(ln.quantity * ln.unit_price)
        _sub(dl, "PrezzoTotale", _money(line_total))
        _sub(dl, "AliquotaIVA", f"{ln.vat_rate:.2f}")
        if ln.natura:
            _sub(dl, "Natura", ln.natura)
        by_rate[ln.vat_rate] = by_rate.get(ln.vat_rate, Decimal(0)) + line_total
    for rate in sorted(by_rate):
        imp = _q2(by_rate[rate])
        rie = _sub(dbs, "DatiRiepilogo")
        _sub(rie, "AliquotaIVA", f"{rate:.2f}")
        _sub(rie, "ImponibileImporto", _money(imp))
        _sub(rie, "Imposta", _money(_q2(imp * rate / Decimal(100))))
    if inv.parent_invoice_id is not None:
        # TD04: link the corrected invoice.
        fc = ET.SubElement(dg, "DatiFattureCollegate")
        _sub(fc, "IdDocumento", str(inv.parent_invoice_id))
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


async def _allocate_number(
    session: AsyncSession, *, org_id: uuid.UUID, series: str, year: int
) -> int:
    """Concurrency-safe: lock (or create) the per-(org,series,year)
    counter row FOR UPDATE; numbers are sequential and never reused."""
    counter = (
        await session.execute(
            select(InvoiceCounter)
            .where(
                InvoiceCounter.org_id == org_id,
                InvoiceCounter.series == series,
                InvoiceCounter.year == year,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if counter is None:
        try:
            async with session.begin_nested():
                counter = InvoiceCounter(org_id=org_id, series=series, year=year, last_number=0)
                session.add(counter)
                await session.flush()
        except IntegrityError:
            pass
        counter = (
            await session.execute(
                select(InvoiceCounter)
                .where(
                    InvoiceCounter.org_id == org_id,
                    InvoiceCounter.series == series,
                    InvoiceCounter.year == year,
                )
                .with_for_update()
            )
        ).scalar_one()
    counter.last_number += 1
    await session.flush()
    return counter.last_number


async def transmit(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    invoice_id: uuid.UUID,
    progressivo: str | None = None,
    channel: SdiChannel | None = None,
) -> Invoice:
    inv = await get_invoice(session, org_id=org_id, invoice_id=invoice_id)
    _require_draft(inv)
    await require_role(session, org_id, actor_id, Role.member)
    fiscal = await get_fiscal_profile(session, org_id=org_id)
    client = await _client(session, inv.client_tag_id)
    lines = await list_lines(session, org_id=org_id, invoice_id=invoice_id)
    _validate(fiscal, client, lines)
    assert fiscal is not None  # _validate raised otherwise  # noqa: S101
    totals = _compute_totals(lines)
    inv.taxable, inv.vat, inv.total = totals.taxable, totals.vat, totals.total
    number = await _allocate_number(session, org_id=org_id, series=inv.series, year=inv.year)
    inv.number = number
    inv.issued_at = dt.datetime.now(tz=dt.UTC)
    inv.xml = _build_xml(inv, fiscal, client, lines, progressivo or f"{inv.year}{number:05d}")
    inv.state = InvoiceState.transmitted
    ch = channel or get_channel()
    res = ch.transmit(xml=inv.xml, invoice_id=str(inv.id))
    inv.identificativo_sdi = res.identificativo_sdi
    inv.conservation_status = res.conservation
    inv.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="invoice",
        entity_id=inv.id,
        action="transmit",
        diff={"number": f"{inv.series}{number}", "channel": res.channel},
    )
    return inv


async def create_credit_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    parent_invoice_id: uuid.UUID,
    causale: str | None = None,
) -> Invoice:
    """TD04 credit note linked to a transmitted invoice (ADR-0009: the
    only post-emission correction). Copies the parent's lines."""
    parent = await get_invoice(session, org_id=org_id, invoice_id=parent_invoice_id)
    if parent.state is InvoiceState.draft:
        raise ConflictError(MessageCode.INVOICE_NOT_DRAFT)
    note = await create_draft(
        session,
        org_id=org_id,
        actor_id=actor_id,
        client_tag_id=parent.client_tag_id,
        year=parent.year,
        series=parent.series,
        causale=causale,
        document_type=DocumentType.TD04,
        kind=InvoiceKind.credit_note,
        parent_invoice_id=parent.id,
    )
    for ln in await list_lines(session, org_id=org_id, invoice_id=parent.id):
        await add_line(
            session,
            org_id=org_id,
            actor_id=actor_id,
            invoice_id=note.id,
            description=ln.description,
            unit_price=ln.unit_price,
            quantity=ln.quantity,
            vat_rate=ln.vat_rate,
            natura=ln.natura,
        )
    return note


async def mark_paid(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    invoice_id: uuid.UUID,
) -> Invoice:
    """Payment is operational reconciliation, not document content, so
    it is allowed post-emission (does not break immutability)."""
    inv = await get_invoice(session, org_id=org_id, invoice_id=invoice_id)
    await require_role(session, org_id, actor_id, Role.member)
    inv.payment_status = PaymentStatus.paid
    inv.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="invoice",
        entity_id=invoice_id,
        action="mark_paid",
    )
    return inv


_RECEIPT_MAP: dict[str, tuple[SdiStatus, InvoiceState, ConservationStatus]] = {
    "RC": (SdiStatus.RC, InvoiceState.delivered, ConservationStatus.ade_covered),
    "MC": (SdiStatus.MC, InvoiceState.delivered, ConservationStatus.ade_covered),
    "AT": (SdiStatus.AT, InvoiceState.delivered, ConservationStatus.ade_covered),
    "NS": (SdiStatus.NS, InvoiceState.rejected, ConservationStatus.out_of_coverage),
}


async def ingest_receipt(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    identificativo_sdi: str,
    outcome: str,
) -> Invoice:
    """Correlate an SdI push notification to the tenant by
    ``IdentificativoSdI`` (ADR-0011) and apply the active-cycle outcome
    (RC/MC/NS/AT). SdI-transited invoices become AdE-covered
    (ADR-0010)."""
    if outcome not in _RECEIPT_MAP:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    inv = (
        await session.execute(
            select(Invoice).where(Invoice.identificativo_sdi == identificativo_sdi)
        )
    ).scalar_one_or_none()
    if inv is None:
        raise NotFoundError(MessageCode.INVOICE_NOT_FOUND)
    sdi, state, cons = _RECEIPT_MAP[outcome]
    inv.sdi_status = sdi
    inv.state = state
    inv.conservation_status = cons
    inv.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="invoice",
        entity_id=inv.id,
        action="receipt",
        diff={"outcome": outcome},
    )
    return inv


async def list_invoices(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    state: InvoiceState | None = None,
    client_tag_id: uuid.UUID | None = None,
) -> list[Invoice]:
    stmt = select(Invoice)
    if state is not None:
        stmt = stmt.where(Invoice.state == state)
    if client_tag_id is not None:
        stmt = stmt.where(Invoice.client_tag_id == client_tag_id)
    stmt = stmt.order_by(Invoice.year.desc(), Invoice.number.desc().nullslast())
    return list((await session.execute(stmt)).scalars().all())
