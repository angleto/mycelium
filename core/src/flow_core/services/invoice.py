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
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete, func, select
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
    IssuerProfile,
    PaymentStatus,
    SdiStatus,
)
from flow_core.models.membership import Role
from flow_core.sdi_channel import SdiChannel, get_channel
from flow_core.services import audit

# Pure formatting / tax-math / XML helpers live in invoice_format
# (#54). Imported with their original names so every call site in this
# module is unchanged. ``BOLLO_DICITURA`` is re-exported because
# invoice_pdf imports it from here.
from flow_core.services.invoice_format import (
    FORFETTARIO_CAUSALE,
    Totals,
    _build_xml,
    _compute_totals,
    _effective_iban,
    _is_forfettario,
    _resolve_line_tax,
)
from flow_core.services.rbac import require_role

# --- issuer profiles (the invoice "intestazione") ---

_PROFILE_FIELDS = frozenset(
    {
        "label",
        "denominazione",
        "piva",
        "codice_fiscale",
        "regime_fiscale",
        "paese",
        "indirizzo",
        "cap",
        "comune",
        "provincia",
        "nazione",
        "rea",
        "default_iban",
    }
)


async def list_issuer_profiles(session: AsyncSession, *, org_id: uuid.UUID) -> list[IssuerProfile]:
    return list(
        (
            await session.execute(
                select(IssuerProfile).order_by(IssuerProfile.is_default.desc(), IssuerProfile.label)
            )
        )
        .scalars()
        .all()
    )


async def get_issuer_profile(
    session: AsyncSession, *, org_id: uuid.UUID, profile_id: uuid.UUID
) -> IssuerProfile:
    p = (
        await session.execute(select(IssuerProfile).where(IssuerProfile.id == profile_id))
    ).scalar_one_or_none()
    if p is None:
        raise NotFoundError(MessageCode.FISCAL_PROFILE_REQUIRED, detail="profile")
    return p


async def get_default_issuer_profile(
    session: AsyncSession, *, org_id: uuid.UUID
) -> IssuerProfile | None:
    return (
        await session.execute(select(IssuerProfile).where(IssuerProfile.is_default.is_(True)))
    ).scalar_one_or_none()


async def _clear_default(session: AsyncSession, *, except_id: uuid.UUID | None = None) -> None:
    rows = (
        (await session.execute(select(IssuerProfile).where(IssuerProfile.is_default.is_(True))))
        .scalars()
        .all()
    )
    for r in rows:
        if except_id is None or r.id != except_id:
            r.is_default = False
    await session.flush()


async def create_issuer_profile(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    label: str,
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
    rea: str | None = None,
    default_iban: str | None = None,
    is_default: bool = False,
) -> IssuerProfile:
    await require_role(session, org_id, actor_id, Role.admin)
    # The first profile is always the default; an explicit default
    # demotes the others (partial unique index: one default per org).
    existing = await list_issuer_profiles(session, org_id=org_id)
    make_default = is_default or not existing
    if make_default:
        await _clear_default(session)
    p = IssuerProfile(
        org_id=org_id,
        label=label,
        denominazione=denominazione,
        piva=piva,
        codice_fiscale=codice_fiscale,
        regime_fiscale=regime_fiscale,
        paese=paese,
        indirizzo=indirizzo,
        cap=cap,
        comune=comune,
        provincia=provincia,
        nazione=nazione,
        rea=rea,
        default_iban=default_iban,
        is_default=make_default,
    )
    session.add(p)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="issuer_profile",
        entity_id=p.id,
        action="create",
    )
    return p


async def update_issuer_profile(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    profile_id: uuid.UUID,
    values: dict[str, object],
    is_default: bool | None = None,
) -> IssuerProfile:
    await require_role(session, org_id, actor_id, Role.admin)
    p = await get_issuer_profile(session, org_id=org_id, profile_id=profile_id)
    unknown = set(values) - _PROFILE_FIELDS
    if unknown:
        raise DomainError(MessageCode.DOMAIN_ERROR, detail=", ".join(sorted(unknown)))
    for field, value in values.items():
        setattr(p, field, value)
    # Promoting to default demotes the rest; the default is moved away
    # only via set_default_issuer_profile (an org keeps exactly one).
    if is_default is True and not p.is_default:
        await _clear_default(session, except_id=p.id)
        p.is_default = True
    p.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="issuer_profile",
        entity_id=p.id,
        action="update",
    )
    return p


async def set_default_issuer_profile(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    profile_id: uuid.UUID,
) -> IssuerProfile:
    await require_role(session, org_id, actor_id, Role.admin)
    p = await get_issuer_profile(session, org_id=org_id, profile_id=profile_id)
    await _clear_default(session, except_id=p.id)
    p.is_default = True
    p.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="issuer_profile",
        entity_id=p.id,
        action="set_default",
    )
    return p


async def delete_issuer_profile(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    profile_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.admin)
    p = await get_issuer_profile(session, org_id=org_id, profile_id=profile_id)
    used = (
        await session.execute(
            select(func.count()).select_from(Invoice).where(Invoice.issuer_profile_id == profile_id)
        )
    ).scalar_one()
    if used:
        # FK is ON DELETE RESTRICT; surface a friendly domain error
        # instead of a raw IntegrityError. Drafts keep their issuer.
        raise ConflictError(MessageCode.ISSUER_PROFILE_IN_USE)
    profiles = await list_issuer_profiles(session, org_id=org_id)
    if p.is_default and len(profiles) > 1:
        raise ConflictError(MessageCode.ISSUER_PROFILE_SOLE_DEFAULT)
    await session.execute(delete(IssuerProfile).where(IssuerProfile.id == profile_id))
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="issuer_profile",
        entity_id=profile_id,
        action="delete",
    )


async def set_conservation_adhesion(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    profile_id: uuid.UUID,
    adhesion: str,
) -> IssuerProfile:
    """Track the AdE free-conservation adhesion (ADR-0010), per issuer
    identity (it is per P.IVA); Flow guides it, it cannot adhere on the
    tenant's behalf."""
    await require_role(session, org_id, actor_id, Role.admin)
    p = await get_issuer_profile(session, org_id=org_id, profile_id=profile_id)
    p.conservation_adhesion = ConservationAdhesion(adhesion)
    p.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="issuer_profile",
        entity_id=p.id,
        action="conservation_adhesion",
        diff={"adhesion": adhesion},
    )
    return p


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
    issuer_profile_id: uuid.UUID | None = None,
    document_type: DocumentType = DocumentType.TD01,
    kind: InvoiceKind = InvoiceKind.invoice,
    parent_invoice_id: uuid.UUID | None = None,
) -> Invoice:
    await require_role(session, org_id, actor_id, Role.member)
    issuer: IssuerProfile | None
    if issuer_profile_id is not None:
        # validate it belongs to this org (RLS-scoped lookup)
        issuer = await get_issuer_profile(session, org_id=org_id, profile_id=issuer_profile_id)
    else:
        issuer = await get_default_issuer_profile(session, org_id=org_id)
        issuer_profile_id = issuer.id if issuer is not None else None
    # Forfettario (RF19): default the mandatory L.190/2014 causale when
    # the caller gave none (an explicit causale is always honoured).
    if causale is None and _is_forfettario(issuer):
        causale = FORFETTARIO_CAUSALE
    inv = Invoice(
        org_id=org_id,
        client_tag_id=client_tag_id,
        issuer_profile_id=issuer_profile_id,
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
    # Resolve and freeze the effective payment IBAN now so it is
    # visible/editable on the draft (precedence: invoice > client >
    # issuer). The client may not have a profile yet at draft time;
    # that is fine, update_draft re-resolves while still empty.
    cp = (
        await session.execute(select(ClientProfile).where(ClientProfile.tag_id == client_tag_id))
    ).scalar_one_or_none()
    iban, _src = _effective_iban(inv, cp, issuer)
    if iban is not None:
        inv.payment_iban = iban
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


_DRAFT_UPDATABLE = frozenset(
    {
        "client_tag_id",
        "issuer_profile_id",
        "series",
        "currency",
        "causale",
        "notes",
        "payment_iban",
        "payment_due_date",
    }
)


async def update_draft(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    invoice_id: uuid.UUID,
    values: dict[str, object],
) -> Invoice:
    """Edit invoice-level fields while the document is still a draft.

    After transmission the document is append-only (ADR-0009): the only
    correction is a TD04 credit note. Mirrors ``transmit``/``mark_paid``
    (direct mutation + version bump) rather than optimistic_update,
    keeping parity with this module's conventions."""
    inv = await get_invoice(session, org_id=org_id, invoice_id=invoice_id)
    _require_draft(inv)
    await require_role(session, org_id, actor_id, Role.member)
    unknown = set(values) - _DRAFT_UPDATABLE
    if unknown:
        raise DomainError(MessageCode.DOMAIN_ERROR, detail=", ".join(sorted(unknown)))
    for field, value in values.items():
        setattr(inv, field, value)
    await session.flush()
    issuer = await _resolve_issuer(session, org_id=org_id, inv=inv)
    # Re-resolve the effective IBAN only while still empty (an explicit
    # invoice IBAN, once set, is never overwritten by client/issuer
    # defaults). The issuer/client may have changed in this same patch.
    if not inv.payment_iban:
        cp = (
            await session.execute(
                select(ClientProfile).where(ClientProfile.tag_id == inv.client_tag_id)
            )
        ).scalar_one_or_none()
        iban, _src = _effective_iban(inv, cp, issuer)
        if iban is not None:
            inv.payment_iban = iban
    # The issuer (hence regime, bollo and forfettario-ness) may have
    # changed: keep taxable/vat/bollo/total consistent.
    await _persist_totals(session, org_id=org_id, inv=inv)
    inv.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="invoice",
        entity_id=inv.id,
        action="update_draft",
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
    vat_rate: Decimal | None = None,
    natura: str | None = None,
) -> InvoiceLine:
    inv = await get_invoice(session, org_id=org_id, invoice_id=invoice_id)
    _require_draft(inv)
    await require_role(session, org_id, actor_id, Role.member)
    issuer = await _resolve_issuer(session, org_id=org_id, inv=inv)
    vat_rate, natura = _resolve_line_tax(issuer, vat_rate, natura)
    # max(line_no)+1, not count+1: deletions leave gaps and count+1
    # would collide with the uq_invoice_lines (invoice_id, line_no).
    next_no = (
        await session.execute(
            select(func.coalesce(func.max(InvoiceLine.line_no), 0)).where(
                InvoiceLine.invoice_id == invoice_id
            )
        )
    ).scalar_one() + 1
    line = InvoiceLine(
        org_id=org_id,
        invoice_id=invoice_id,
        line_no=next_no,
        description=description,
        quantity=quantity,
        unit_price=unit_price,
        vat_rate=vat_rate,
        natura=natura,
    )
    session.add(line)
    await session.flush()
    await _persist_totals(session, org_id=org_id, inv=inv)
    return line


async def update_line(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    invoice_id: uuid.UUID,
    line_id: uuid.UUID,
    description: str,
    unit_price: Decimal,
    quantity: Decimal,
    vat_rate: Decimal | None = None,
    natura: str | None = None,
) -> InvoiceLine:
    inv = await get_invoice(session, org_id=org_id, invoice_id=invoice_id)
    _require_draft(inv)
    await require_role(session, org_id, actor_id, Role.member)
    line = (
        await session.execute(
            select(InvoiceLine).where(
                InvoiceLine.id == line_id, InvoiceLine.invoice_id == invoice_id
            )
        )
    ).scalar_one_or_none()
    if line is None:
        raise NotFoundError(MessageCode.INVOICE_NOT_FOUND, detail="line")
    issuer = await _resolve_issuer(session, org_id=org_id, inv=inv)
    vat_rate, natura = _resolve_line_tax(issuer, vat_rate, natura)
    line.description = description
    line.unit_price = unit_price
    line.quantity = quantity
    line.vat_rate = vat_rate
    line.natura = natura
    await session.flush()
    await _persist_totals(session, org_id=org_id, inv=inv)
    return line


async def delete_line(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    invoice_id: uuid.UUID,
    line_id: uuid.UUID,
) -> None:
    inv = await get_invoice(session, org_id=org_id, invoice_id=invoice_id)
    _require_draft(inv)
    await require_role(session, org_id, actor_id, Role.member)
    line = (
        await session.execute(
            select(InvoiceLine).where(
                InvoiceLine.id == line_id, InvoiceLine.invoice_id == invoice_id
            )
        )
    ).scalar_one_or_none()
    if line is None:
        raise NotFoundError(MessageCode.INVOICE_NOT_FOUND, detail="line")
    await session.execute(delete(InvoiceLine).where(InvoiceLine.id == line_id))
    await session.flush()
    # Re-sequence the survivors to 1..n so line numbers stay contiguous.
    # Ascending assignment with monotonically non-increasing targets
    # never transiently violates uq_invoice_lines (invoice_id, line_no).
    rest = list(
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
    for idx, ln in enumerate(rest, start=1):
        if ln.line_no != idx:
            ln.line_no = idx
    await session.flush()
    await _persist_totals(session, org_id=org_id, inv=inv)


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


async def _resolve_issuer(
    session: AsyncSession, *, org_id: uuid.UUID, inv: Invoice
) -> IssuerProfile | None:
    """The invoice's issuer identity: the explicitly chosen profile, or
    the org default when none was picked."""
    if inv.issuer_profile_id is not None:
        return await get_issuer_profile(session, org_id=org_id, profile_id=inv.issuer_profile_id)
    return await get_default_issuer_profile(session, org_id=org_id)


async def _persist_totals(session: AsyncSession, *, org_id: uuid.UUID, inv: Invoice) -> Totals:
    """Recompute and store taxable/vat/bollo/total on the draft so they
    stay consistent with the lines and the issuer's regime. Called from
    every mutation that changes lines or the issuer."""
    issuer = await _resolve_issuer(session, org_id=org_id, inv=inv)
    lines = await list_lines(session, org_id=org_id, invoice_id=inv.id)
    totals = _compute_totals(lines, issuer)
    inv.taxable = totals.taxable
    inv.vat = totals.vat
    inv.bollo = totals.bollo
    inv.total = totals.total
    await session.flush()
    return totals


async def _client(session: AsyncSession, client_tag_id: uuid.UUID) -> ClientProfile:
    cp = (
        await session.execute(select(ClientProfile).where(ClientProfile.tag_id == client_tag_id))
    ).scalar_one_or_none()
    if cp is None:
        raise DomainError(MessageCode.INVOICE_INVALID, detail="client profile missing")
    return cp


def _validate(
    fiscal: IssuerProfile | None,
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
    # The chosen issuer identity (default if none); its header is frozen
    # into inv.xml below, so later edits never touch this document.
    fiscal = await _resolve_issuer(session, org_id=org_id, inv=inv)
    client = await _client(session, inv.client_tag_id)
    lines = await list_lines(session, org_id=org_id, invoice_id=invoice_id)
    _validate(fiscal, client, lines)
    assert fiscal is not None  # _validate raised otherwise  # noqa: S101
    totals = _compute_totals(lines, fiscal)
    inv.taxable, inv.vat, inv.bollo, inv.total = (
        totals.taxable,
        totals.vat,
        totals.bollo,
        totals.total,
    )
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


# --- draft preview (live XML / PDF / structured JSON) ---


@dataclass(frozen=True)
class InvoicePreview:
    """Everything that will appear on the document, resolved once so the
    frontend renders it without re-deriving. Tolerant of an incomplete
    draft (missing fiscal fields are simply None/empty here; the XML and
    PDF previews still validate and surface the domain error)."""

    issuer: IssuerProfile | None
    client: ClientProfile | None
    lines: list[InvoiceLine]
    totals: Totals
    effective_iban: str | None
    iban_source: str | None
    is_forfettario: bool
    number: str


async def _would_be_number(
    session: AsyncSession, *, org_id: uuid.UUID, series: str, year: int
) -> int:
    """The number this draft would get at transmit, WITHOUT allocating
    (no counter mutation, no lock): last_number + 1, or 1 if none yet.
    For display only; the authoritative allocation stays in transmit."""
    counter = (
        await session.execute(
            select(InvoiceCounter).where(
                InvoiceCounter.org_id == org_id,
                InvoiceCounter.series == series,
                InvoiceCounter.year == year,
            )
        )
    ).scalar_one_or_none()
    return (counter.last_number + 1) if counter is not None else 1


async def _gather_preview(
    session: AsyncSession, *, org_id: uuid.UUID, inv: Invoice
) -> InvoicePreview:
    issuer = await _resolve_issuer(session, org_id=org_id, inv=inv)
    client = (
        await session.execute(
            select(ClientProfile).where(ClientProfile.tag_id == inv.client_tag_id)
        )
    ).scalar_one_or_none()
    lines = await list_lines(session, org_id=org_id, invoice_id=inv.id)
    totals = _compute_totals(lines, issuer)
    iban, src = _effective_iban(inv, client, issuer)
    n = inv.number
    if n is None:
        n = await _would_be_number(session, org_id=org_id, series=inv.series, year=inv.year)
    return InvoicePreview(
        issuer=issuer,
        client=client,
        lines=lines,
        totals=totals,
        effective_iban=iban,
        iban_source=src,
        is_forfettario=_is_forfettario(issuer),
        number=f"{inv.series}{n}",
    )


async def get_preview(
    session: AsyncSession, *, org_id: uuid.UUID, invoice_id: uuid.UUID
) -> tuple[Invoice, InvoicePreview]:
    """Structured preview of the full document. Does NOT validate: an
    incomplete draft still returns (so the UI can show what is filled
    and what is missing)."""
    inv = await get_invoice(session, org_id=org_id, invoice_id=invoice_id)
    return inv, await _gather_preview(session, org_id=org_id, inv=inv)


async def get_xml_preview(
    session: AsyncSession, *, org_id: uuid.UUID, invoice_id: uuid.UUID
) -> str:
    """The transited XML if already transmitted, else a LIVE preview
    built from the current draft. Validates first (the preview must
    reflect a sendable document): missing fiscal data raises the domain
    error so the UI shows exactly what is missing, never a 404."""
    inv = await get_invoice(session, org_id=org_id, invoice_id=invoice_id)
    if inv.xml is not None:
        return inv.xml
    p = await _gather_preview(session, org_id=org_id, inv=inv)
    _validate(p.issuer, p.client or ClientProfile(), p.lines)
    assert p.issuer is not None and p.client is not None  # _validate raised  # noqa: S101
    # Use the persisted (consistent) totals; ANTEPRIMA progressivo +
    # the would-be number make it a faithful, non-allocating preview.
    inv.taxable, inv.vat, inv.bollo, inv.total = (
        p.totals.taxable,
        p.totals.vat,
        p.totals.bollo,
        p.totals.total,
    )
    return _build_xml(inv, p.issuer, p.client, p.lines, "ANTEPRIMA", numero_override=p.number)


async def render_pdf(
    session: AsyncSession, *, org_id: uuid.UUID, invoice_id: uuid.UUID
) -> tuple[str, bytes]:
    """Courtesy A4 PDF of the document (draft preview or emitted).
    Validates like the XML preview; returns (number, pdf_bytes)."""
    from flow_core.services.invoice_pdf import build_pdf

    inv = await get_invoice(session, org_id=org_id, invoice_id=invoice_id)
    p = await _gather_preview(session, org_id=org_id, inv=inv)
    _validate(p.issuer, p.client or ClientProfile(), p.lines)
    assert p.issuer is not None and p.client is not None  # _validate raised  # noqa: S101
    pdf = build_pdf(inv, p.issuer, p.client, p.lines, p.totals)
    return p.number, pdf
