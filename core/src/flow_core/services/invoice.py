"""Italian electronic invoicing service (docs/adr/0009, 0010, 0011,
FR-9).

Legally load-bearing invariants, enforced here:
- only ``draft`` is mutable; after emission the document is
  append-only, correction is a TD04 credit note (ADR-0009);
- the progressive number per (issuer_profile, series, year) is
  allocated concurrency-safe (counter row, ``FOR UPDATE``) only at
  draft -> transmitted, in the same transaction, never reused; the
  series defaults to the client's own sezionale (per-client numbering);
- the tenant identity is in the FatturaPA payload, not the channel
  (ADR-0011); ``ManualExportChannel`` invoices are out of AdE free
  conservation (ADR-0010), SdI-transited ones become covered.
FatturaPA 1.2 XML is built deterministically and structurally +
arithmetically validated (full XSD validation is a hardening add-on).
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import ConflictError, DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.it_provinces import is_valid_provincia
from flow_core.models.client_profile import ClientProfile
from flow_core.models.invoice import (
    BuyerVerdict,
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
    SdiTransmissionCounter,
)
from flow_core.models.membership import Role
from flow_core.models.sdi_notification import InvoiceNotification
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
from flow_core.services.invoice_xsd import validate_fatturapa
from flow_core.services.payment_methods import (
    validate_condizioni,
    validate_modalita,
    validate_terms_days,
)
from flow_core.services.rbac import require_role
from flow_core.services.sdi_mandate import get_active_mandate
from flow_core.services.sdi_transport import fatturapa_filename, transmission_progressivo
from flow_core.vat import is_valid_vat_code, normalize_vat

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
        "riferimento_normativo",
        "nome",
        "cognome",
        "pec",
        "email",
        "telefono",
        "fax",
        "default_condizioni_pagamento",
        "default_modalita_pagamento",
        "default_payment_terms_days",
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
    riferimento_normativo: str | None = None,
    nome: str | None = None,
    cognome: str | None = None,
    pec: str | None = None,
    email: str | None = None,
    telefono: str | None = None,
    fax: str | None = None,
    default_condizioni_pagamento: str | None = None,
    default_modalita_pagamento: str | None = None,
    default_payment_terms_days: int | None = None,
    is_default: bool = False,
) -> IssuerProfile:
    await require_role(session, org_id, actor_id, Role.admin)
    # The first profile is always the default; an explicit default
    # demotes the others (partial unique index: one default per org).
    existing = await list_issuer_profiles(session, org_id=org_id)
    make_default = is_default or not existing
    if make_default:
        await _clear_default(session)
    # Normalize a VIES-form P.IVA ("IT13438810015") into IdPaese + bare
    # IdCodice; reject a malformed code (FatturaPA IdCodice is the number
    # only, no country prefix).
    new_paese, piva = normalize_vat(piva, paese)
    paese = new_paese or paese
    if not is_valid_vat_code(piva, paese):
        raise DomainError(MessageCode.FISCAL_PROFILE_REQUIRED, detail=f"piva '{piva}'")
    # Closed enums (FatturaPA tables TPxx/MPxx) and a sane integer range
    # for the net-days default: catch typos here before they reach SdI.
    default_condizioni_pagamento = validate_condizioni(default_condizioni_pagamento)
    default_modalita_pagamento = validate_modalita(default_modalita_pagamento)
    default_payment_terms_days = validate_terms_days(default_payment_terms_days)
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
        riferimento_normativo=riferimento_normativo,
        nome=nome,
        cognome=cognome,
        pec=pec,
        email=email,
        telefono=telefono,
        fax=fax,
        default_condizioni_pagamento=default_condizioni_pagamento,
        default_modalita_pagamento=default_modalita_pagamento,
        default_payment_terms_days=default_payment_terms_days,
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
    # Closed-enum validation BEFORE we apply the update: SdI rejects an
    # unknown TPxx/MPxx so we never let one land in the row. ``None``
    # passes through (clearing the override).
    if "default_condizioni_pagamento" in values:
        values["default_condizioni_pagamento"] = validate_condizioni(
            values["default_condizioni_pagamento"]  # type: ignore[arg-type]
        )
    if "default_modalita_pagamento" in values:
        values["default_modalita_pagamento"] = validate_modalita(
            values["default_modalita_pagamento"]  # type: ignore[arg-type]
        )
    if "default_payment_terms_days" in values:
        values["default_payment_terms_days"] = validate_terms_days(
            values["default_payment_terms_days"]  # type: ignore[arg-type]
        )
    for field, value in values.items():
        setattr(p, field, value)
    if "piva" in values or "paese" in values:
        new_paese, p.piva = normalize_vat(p.piva, p.paese)
        if new_paese is not None:
            p.paese = new_paese
        if not is_valid_vat_code(p.piva, p.paese):
            raise DomainError(MessageCode.FISCAL_PROFILE_REQUIRED, detail=f"piva '{p.piva}'")
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


# --- invoice counter management (import from another system) ---


@dataclass(frozen=True)
class InvoiceCounterRow:
    """A counter row plus the maximum number already emitted under the
    same key. The UI needs both to make the lower bound obvious."""

    issuer_profile_id: uuid.UUID
    series: str
    year: int
    last_number: int
    max_emitted: int


async def list_counters(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    issuer_profile_id: uuid.UUID,
) -> list[InvoiceCounterRow]:
    """Every counter row owned by this issuer, decorated with the
    ``max_emitted`` number for that (series, year). ``max_emitted`` is the
    floor any override must respect (we never decrement below an already
    issued number, otherwise the next allocation collides with the unique
    constraint on ``invoices``)."""
    await get_issuer_profile(session, org_id=org_id, profile_id=issuer_profile_id)
    rows = list(
        (
            await session.execute(
                select(InvoiceCounter)
                .where(InvoiceCounter.issuer_profile_id == issuer_profile_id)
                .order_by(InvoiceCounter.year.desc(), InvoiceCounter.series)
            )
        )
        .scalars()
        .all()
    )
    out: list[InvoiceCounterRow] = []
    for c in rows:
        max_n = (
            await session.execute(
                select(func.coalesce(func.max(Invoice.number), 0)).where(
                    Invoice.issuer_profile_id == issuer_profile_id,
                    Invoice.series == c.series,
                    Invoice.year == c.year,
                )
            )
        ).scalar_one()
        out.append(
            InvoiceCounterRow(
                issuer_profile_id=issuer_profile_id,
                series=c.series,
                year=c.year,
                last_number=c.last_number,
                max_emitted=int(max_n or 0),
            )
        )
    return out


async def set_counter(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    issuer_profile_id: uuid.UUID,
    series: str,
    year: int,
    last_number: int,
) -> InvoiceCounterRow:
    """Override the progressive counter for a (issuer, series, year)
    triple. Used when migrating from another billing system: typically
    the tenant has already emitted N invoices elsewhere and wants Flow
    to continue from N+1, so we set ``last_number = N``.

    Hard invariant: the new value MUST be ``>= max(number)`` already
    persisted on ``invoices`` under the same key. Going below would let
    the next allocation reuse an existing number, breaking both
    ``uq_invoices_issuer`` AND the legal "never reused" promise. We
    raise ``ConflictError`` (with the floor as detail) instead.

    Locks the counter ``FOR UPDATE`` like ``_allocate_number`` so a
    concurrent transmit cannot interleave between the floor check and
    the assignment."""
    await require_role(session, org_id, actor_id, Role.admin)
    if last_number < 0:
        raise DomainError(MessageCode.DOMAIN_ERROR, detail=f"last_number '{last_number}'")
    issuer = await get_issuer_profile(session, org_id=org_id, profile_id=issuer_profile_id)
    counter = (
        await session.execute(
            select(InvoiceCounter)
            .where(
                InvoiceCounter.issuer_profile_id == issuer_profile_id,
                InvoiceCounter.series == series,
                InvoiceCounter.year == year,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    max_n = (
        await session.execute(
            select(func.coalesce(func.max(Invoice.number), 0)).where(
                Invoice.issuer_profile_id == issuer_profile_id,
                Invoice.series == series,
                Invoice.year == year,
            )
        )
    ).scalar_one()
    floor = int(max_n or 0)
    if last_number < floor:
        raise ConflictError(MessageCode.INVOICE_INVALID, detail=f"last_number<{floor}")
    if counter is None:
        counter = InvoiceCounter(
            org_id=issuer.org_id,
            issuer_profile_id=issuer_profile_id,
            series=series,
            year=year,
            last_number=last_number,
        )
        session.add(counter)
    else:
        counter.last_number = last_number
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="invoice_counter",
        entity_id=issuer_profile_id,
        action="set_counter",
        diff={"series": series, "year": year, "last_number": last_number},
    )
    return InvoiceCounterRow(
        issuer_profile_id=issuer_profile_id,
        series=series,
        year=year,
        last_number=last_number,
        max_emitted=floor,
    )


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


_SERIES_BASE_MAX = 8


def _derive_series_base(name: str) -> str:
    """A client's default sezionale code: the name uppercased, stripped to
    [A-Z0-9], capped at 8 chars (kept short so ``series + number`` stays a sane
    FatturaPA Numero). Falls back to "CLI" when the name has no usable chars."""
    base = re.sub(r"[^A-Z0-9]", "", (name or "").upper())[:_SERIES_BASE_MAX]
    return base or "CLI"


async def _ensure_client_series(
    session: AsyncSession, *, org_id: uuid.UUID, client: ClientProfile
) -> str:
    """The client's invoice sezionale, deriving + persisting a unique one on
    first use. Per-client numbering is a series-per-client (each client an
    independent progressive sequence): the code defaults to a sanitized prefix
    of the name, suffixed with a counter if another client already took it, so
    two clients never collide on one sequence. Stored on the client, so it is
    stable across that client's invoices."""
    if client.invoice_series:
        return client.invoice_series
    taken = set(
        (
            await session.execute(
                select(ClientProfile.invoice_series).where(
                    ClientProfile.invoice_series.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )
    base = _derive_series_base(client.ragione_sociale)
    code = base
    n = 2
    while code in taken:
        code = f"{base}{n}"
        n += 1
    client.invoice_series = code
    await session.flush()
    return code


async def create_draft(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    client_tag_id: uuid.UUID,
    year: int | None = None,
    series: str | None = None,
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
    cp = (
        await session.execute(select(ClientProfile).where(ClientProfile.tag_id == client_tag_id))
    ).scalar_one_or_none()
    # Series defaults to the client's own sezionale (per-client numbering): each
    # client gets an independent progressive sequence under (issuer, series,
    # year). An explicit series always wins; with no client profile yet, "A".
    if series is None:
        series = (
            await _ensure_client_series(session, org_id=org_id, client=cp)
            if cp is not None
            else "A"
        )
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
        # client_tag_id and issuer_profile_id are intentionally NOT in
        # this set: a draft's billing identity (which client is billed,
        # under which VAT subject) is frozen at create_draft. Mutating
        # either after the fact would silently rewire the per-client
        # sezionale + the (issuer, series, year) counter underneath an
        # existing draft, which is exactly the source of confusion the
        # product owner asked us to remove. The supported workflow is:
        # delete the draft and create a fresh one.
        "series",
        "currency",
        "causale",
        "notes",
        "payment_iban",
        "payment_due_date",
        "condizioni_pagamento",
        "modalita_pagamento",
        "payment_terms_days",
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
    # Validate the closed-enum / range fields before we touch the row.
    if "condizioni_pagamento" in values:
        values["condizioni_pagamento"] = validate_condizioni(
            values["condizioni_pagamento"]  # type: ignore[arg-type]
        )
    if "modalita_pagamento" in values:
        values["modalita_pagamento"] = validate_modalita(
            values["modalita_pagamento"]  # type: ignore[arg-type]
        )
    if "payment_terms_days" in values:
        values["payment_terms_days"] = validate_terms_days(
            values["payment_terms_days"]  # type: ignore[arg-type]
        )
    for field, value in values.items():
        setattr(inv, field, value)
    await session.flush()
    issuer = await _resolve_issuer(session, org_id=org_id, inv=inv)
    cp = (
        await session.execute(
            select(ClientProfile).where(ClientProfile.tag_id == inv.client_tag_id)
        )
    ).scalar_one_or_none()
    # Re-resolve the effective IBAN only while still empty (an explicit
    # invoice IBAN, once set, is never overwritten by client/issuer
    # defaults). The issuer/client may have changed in this same patch.
    if not inv.payment_iban:
        iban, _src = _effective_iban(inv, cp, issuer)
        if iban is not None:
            inv.payment_iban = iban
    # When the user set net-days but no explicit due date, materialize
    # the due date now so the draft preview shows it. The resolver picks
    # up the days from the client / issuer too; auto-fill only when the
    # field is empty (an explicit user-set date is never overwritten).
    if inv.payment_due_date is None:
        from flow_core.services.payment_methods import resolve_payment as _rp

        resolved = _rp(inv, cp, issuer)
        if resolved.terms_days is not None:
            base = (inv.issued_at or dt.datetime.now(tz=dt.UTC)).date()
            inv.payment_due_date = base + dt.timedelta(days=resolved.terms_days)
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
    # The cedente's IdFiscaleIVA (P.IVA) is mandatory in FatturaPA (the
    # CodiceFiscale alone is not enough for the issuer; the cessionario may
    # have just a CodiceFiscale, the cedente may not).
    if not fiscal.piva:
        missing.append("piva")
    if missing:
        raise DomainError(MessageCode.FISCAL_PROFILE_REQUIRED, detail=", ".join(missing))
    # The cessionario's Sede (Indirizzo/CAP/Comune) is mandatory in
    # FatturaPA; surface a clean domain error here rather than letting the
    # XSD reject an empty Indirizzo with a cryptic pattern message.
    client_missing = [
        f
        for f, v in (
            ("ragione_sociale", client.ragione_sociale),
            ("indirizzo", client.indirizzo),
            ("cap", client.cap),
            ("comune", client.comune),
        )
        if not v
    ]
    if not (client.id_codice or client.codice_fiscale):
        client_missing.append("piva|codice_fiscale")
    if not (client.codice_destinatario or client.pec):
        client_missing.append("codice_destinatario|pec")
    if client_missing:
        raise DomainError(
            MessageCode.INVOICE_INVALID, detail="client: " + ", ".join(client_missing)
        )
    # Provincia: the XSD only checks the [A-Z]{2} shape; reject a well-formed
    # but nonexistent Italian province before SdI does (scarto).
    if not is_valid_provincia(fiscal.provincia, fiscal.nazione):
        raise DomainError(
            MessageCode.INVOICE_INVALID, detail=f"issuer provincia '{fiscal.provincia}'"
        )
    if not is_valid_provincia(client.provincia, client.nazione):
        raise DomainError(
            MessageCode.INVOICE_INVALID, detail=f"client provincia '{client.provincia}'"
        )
    if not lines:
        raise DomainError(MessageCode.INVOICE_INVALID, detail="no lines")


async def _allocate_number(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    issuer_profile_id: uuid.UUID,
    series: str,
    year: int,
) -> int:
    """Concurrency-safe: lock (or create) the per-(issuer,series,year)
    counter row FOR UPDATE; numbers are sequential and never reused. Keyed by
    the issuer profile (the cedente/prestatore), not the org: each VAT subject
    owns an independent progressive sequence (DPR 633/72 art.21). ``org_id`` is
    not part of the key; it is stamped on a new counter row only to satisfy the
    table's RLS WITH CHECK (tenant isolation)."""
    counter = (
        await session.execute(
            select(InvoiceCounter)
            .where(
                InvoiceCounter.issuer_profile_id == issuer_profile_id,
                InvoiceCounter.series == series,
                InvoiceCounter.year == year,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if counter is None:
        try:
            async with session.begin_nested():
                counter = InvoiceCounter(
                    org_id=org_id,
                    issuer_profile_id=issuer_profile_id,
                    series=series,
                    year=year,
                    last_number=0,
                )
                session.add(counter)
                await session.flush()
        except IntegrityError:
            pass
        counter = (
            await session.execute(
                select(InvoiceCounter)
                .where(
                    InvoiceCounter.issuer_profile_id == issuer_profile_id,
                    InvoiceCounter.series == series,
                    InvoiceCounter.year == year,
                )
                .with_for_update()
            )
        ).scalar_one()
    counter.last_number += 1
    await session.flush()
    return counter.last_number


async def _allocate_transmission_seq(session: AsyncSession, *, intermediary_id: str) -> int:
    """Concurrency-safe per-intermediary monotonic sequence for the SdI file
    name + ProgressivoInvio (unique per trasmittente, across all tenants since
    one channel serves all). Mirrors ``_allocate_number``: lock (or create)
    the counter row FOR UPDATE; never reused."""
    counter = (
        await session.execute(
            select(SdiTransmissionCounter)
            .where(SdiTransmissionCounter.intermediary_id == intermediary_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if counter is None:
        try:
            async with session.begin_nested():
                counter = SdiTransmissionCounter(intermediary_id=intermediary_id, last_number=0)
                session.add(counter)
                await session.flush()
        except IntegrityError:
            pass
        counter = (
            await session.execute(
                select(SdiTransmissionCounter)
                .where(SdiTransmissionCounter.intermediary_id == intermediary_id)
                .with_for_update()
            )
        ).scalar_one()
    counter.last_number += 1
    await session.flush()
    return counter.last_number


async def _resolve_collegata(
    session: AsyncSession, *, org_id: uuid.UUID, inv: Invoice
) -> tuple[str, dt.date] | None:
    """For a TD04, the corrected invoice's FISCAL number + issue date for
    DatiFattureCollegate (not the internal UUID). None when no parent."""
    if inv.parent_invoice_id is None:
        return None
    parent = await get_invoice(session, org_id=org_id, invoice_id=inv.parent_invoice_id)
    # Match the prominent ``<sezionale>-<counter>`` formatting used in
    # the parent's own <Numero> / PDF header; without the hyphen the
    # IdDocumento on a TD04 doesn't match the human-readable identifier
    # on the corrected invoice (confusing on the receiving side).
    numero = f"{parent.series}-{parent.number}" if parent.number is not None else str(parent.series)
    data = (parent.issued_at or dt.datetime.now(tz=dt.UTC)).date()
    return numero, data


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
    ch = channel or get_channel()
    intermediary = ch.intermediary
    if intermediary is not None:
        # Transmitting via the accredited channel = Flow acts as intermediary
        # for this VAT subject; an active SdiMandate is required (ADR-0011).
        mandate = (
            await get_active_mandate(
                session, org_id=org_id, issuer_profile_id=inv.issuer_profile_id
            )
            if inv.issuer_profile_id is not None
            else None
        )
        if mandate is None:
            raise ConflictError(MessageCode.MANDATE_REQUIRED)
    totals = _compute_totals(lines, fiscal)
    inv.taxable, inv.vat, inv.bollo, inv.total = (
        totals.taxable,
        totals.vat,
        totals.bollo,
        totals.total,
    )
    number = await _allocate_number(
        session, org_id=org_id, issuer_profile_id=fiscal.id, series=inv.series, year=inv.year
    )
    inv.number = number
    inv.issued_at = dt.datetime.now(tz=dt.UTC)
    # The SdI file-name progressivo is max 5 alphanumeric chars (Specifiche
    # SDI, Allegato B). It must also be unique per trasmittente across every
    # file ever sent, which the per-org/per-issuer invoice number is NOT
    # (it resets each year and is scoped differently). So both paths draw a
    # dedicated monotonic per-trasmittente sequence rendered base36 width 5
    # (``transmission_progressivo``); the trasmittente is the accredited
    # channel holder when Flow acts as intermediary, else the cedente itself.
    if intermediary is not None:
        seq = await _allocate_transmission_seq(session, intermediary_id=intermediary.id_codice)
        progressivo_str = transmission_progressivo(seq)
        filename = fatturapa_filename(
            intermediary.id_paese, intermediary.id_codice, progressivo_str
        )
    else:
        cedente_id = fiscal.piva or fiscal.codice_fiscale or ""
        if progressivo is not None:
            progressivo_str = progressivo
        else:
            seq = await _allocate_transmission_seq(session, intermediary_id=cedente_id)
            progressivo_str = transmission_progressivo(seq)
        filename = fatturapa_filename(fiscal.paese, cedente_id, progressivo_str)
    collegata = await _resolve_collegata(session, org_id=org_id, inv=inv)
    xml = _build_xml(
        inv,
        fiscal,
        client,
        lines,
        progressivo_str,
        collegata=collegata,
        intermediary=intermediary,
    )
    # Validate against the official FatturaPA XSD before emission: SdI
    # scarta anything non-conformant, so an invalid document must never
    # leave draft. Surfaced as the domain error the UI already renders.
    xsd_errors = validate_fatturapa(xml)
    if xsd_errors:
        raise DomainError(MessageCode.INVOICE_INVALID, detail="; ".join(xsd_errors[:5]))
    inv.xml = xml
    inv.state = InvoiceState.transmitted
    res = await ch.transmit(xml=xml, invoice_id=str(inv.id), filename=filename)
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
        diff={"number": f"{inv.series}-{number}", "channel": res.channel},
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
        # A credit note must be issued by the SAME cedente as the corrected
        # invoice: inherit the parent's issuer explicitly (not the current org
        # default, which may have changed) so the document is fiscally coherent
        # and shares the parent's (issuer, series, year) numbering sequence.
        issuer_profile_id=parent.issuer_profile_id,
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


# Active-cycle outcomes RC/MC/AT/NS: legacy delivery + conservation effect.
# NE/DT do not alter sdi_status this way (they layer on top, see verdict).
_RECEIPT_MAP: dict[str, tuple[SdiStatus, InvoiceState, ConservationStatus]] = {
    "RC": (SdiStatus.RC, InvoiceState.delivered, ConservationStatus.ade_covered),
    "MC": (SdiStatus.MC, InvoiceState.delivered, ConservationStatus.ade_covered),
    "AT": (SdiStatus.AT, InvoiceState.delivered, ConservationStatus.ade_covered),
    "NS": (SdiStatus.NS, InvoiceState.rejected, ConservationStatus.out_of_coverage),
}

# EC code -> buyer verdict. The XSD restricts EsitoCommittente_Type to these
# two values (annotations: EC01=ACCETTAZIONE, EC02=RIFIUTO).
_EC_VERDICT: dict[str, tuple[BuyerVerdict, InvoiceState]] = {
    "EC01": (BuyerVerdict.accepted, InvoiceState.accepted),
    "EC02": (BuyerVerdict.rejected, InvoiceState.rejected),
}


async def ingest_active_notification(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    parsed: ParsedNotificationLike,
) -> Invoice:
    """Correlate an SdI active-cycle notification to the tenant by
    ``IdentificativoSdI`` (ADR-0011) and dispatch it: RC/MC/AT mark delivery
    + AdE coverage; NS rejects; NE applies the buyer verdict; DT marks
    deemed acceptance (15-day window expired). Every notification, including
    the raw signed XML, is appended to ``invoice_notifications`` for audit;
    the unique ``(invoice_id, kind, message_id)`` index makes SdI retries
    idempotent."""
    inv = (
        await session.execute(
            select(Invoice).where(Invoice.identificativo_sdi == parsed.identificativo_sdi)
        )
    ).scalar_one_or_none()
    if inv is None:
        raise NotFoundError(MessageCode.INVOICE_NOT_FOUND)
    now = dt.datetime.now(tz=dt.UTC)
    payload: dict[str, object] = {}
    if parsed.outcome in _RECEIPT_MAP:
        sdi, state, cons = _RECEIPT_MAP[parsed.outcome]
        inv.sdi_status = sdi
        inv.state = state
        inv.conservation_status = cons
        payload = {"outcome": parsed.outcome}
    elif parsed.outcome == "NE":
        # NE relays the buyer's EsitoCommittente. We layer the verdict on top
        # of the existing delivery state without clobbering RC/MC/AT.
        if parsed.esito not in _EC_VERDICT:
            raise DomainError(
                MessageCode.DOMAIN_ERROR, detail=f"NE missing/unknown Esito: {parsed.esito!r}"
            )
        verdict, state = _EC_VERDICT[parsed.esito]
        inv.sdi_status = SdiStatus.NE
        inv.buyer_verdict = verdict
        inv.buyer_verdict_at = now
        inv.state = state
        payload = {"outcome": "NE", "esito": parsed.esito}
    elif parsed.outcome == "DT":
        # Deemed acceptance (15-day window expired). Only flip the verdict
        # if no explicit NE arrived earlier; preserve a buyer's explicit
        # accept/reject (NE wins over DT).
        inv.sdi_status = SdiStatus.DT
        inv.dt_received_at = now
        if inv.buyer_verdict is BuyerVerdict.none:
            inv.buyer_verdict = BuyerVerdict.deemed_accepted
            inv.buyer_verdict_at = now
            inv.state = InvoiceState.accepted
        payload = {"outcome": "DT"}
    else:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    inv.version += 1

    notif = InvoiceNotification(
        org_id=org_id,
        invoice_id=inv.id,
        kind=parsed.outcome,
        nome_file=parsed.nome_file,
        message_id=parsed.message_id,
        raw_xml=parsed.raw_xml,
        payload=payload,
    )
    session.add(notif)
    try:
        await session.flush()
    except IntegrityError:
        # SdI re-delivered a notification with the same (invoice, kind,
        # message_id); the prior ingest already applied it. Roll back the
        # whole transaction so the verdict columns stay coherent with the
        # audit row that wins (the first insertion).
        await session.rollback()
        return inv

    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="invoice",
        entity_id=inv.id,
        action="sdi_notification",
        diff=payload,
    )
    return inv


# Kept for compatibility with older callers / tests that still pass an
# outcome string instead of a ParsedNotification dataclass. New code should
# use ``ingest_active_notification`` so the audit row gets written.
async def ingest_receipt(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    identificativo_sdi: str,
    outcome: str,
) -> Invoice:
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


# Forward-declared protocol-ish typing helper to avoid an import cycle with
# ``sdi_inbound``. The dispatcher only reads attributes from the parsed
# notification; any object exposing this shape is accepted.
from typing import Protocol  # noqa: E402


class ParsedNotificationLike(Protocol):
    # Read-only attributes (declared as properties so the frozen
    # ``ParsedNotification`` dataclass in ``sdi_inbound`` satisfies the
    # protocol; plain ``x: T`` on a Protocol means *settable* in mypy).
    @property
    def outcome(self) -> str: ...
    @property
    def identificativo_sdi(self) -> str: ...
    @property
    def message_id(self) -> str | None: ...
    @property
    def nome_file(self) -> str | None: ...
    @property
    def esito(self) -> str | None: ...
    @property
    def raw_xml(self) -> bytes: ...


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
    session: AsyncSession, *, issuer_profile_id: uuid.UUID, series: str, year: int
) -> int:
    """The number this draft would get at transmit, WITHOUT allocating
    (no counter mutation, no lock): last_number + 1, or 1 if none yet.
    Keyed per issuer like the real allocation. For display only; the
    authoritative allocation stays in transmit."""
    counter = (
        await session.execute(
            select(InvoiceCounter).where(
                InvoiceCounter.issuer_profile_id == issuer_profile_id,
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
        # Display-only "would-be" number, keyed per issuer like the real
        # allocation. ``issuer`` may be None on an incomplete draft with no
        # profile yet; then there is no sequence to peek, so show 1.
        iid = issuer.id if issuer is not None else inv.issuer_profile_id
        n = (
            await _would_be_number(session, issuer_profile_id=iid, series=inv.series, year=inv.year)
            if iid is not None
            else 1
        )
    return InvoicePreview(
        issuer=issuer,
        client=client,
        lines=lines,
        totals=totals,
        effective_iban=iban,
        iban_source=src,
        is_forfettario=_is_forfettario(issuer),
        # ``<sezionale>-<counter>`` (e.g. ``CYLOCK-2``). The hyphen
        # separator is mirrored verbatim into FatturaPA ``<Numero>`` and
        # the PDF header so a human reader can tell where the per-
        # client series ends and the progressive starts; without it,
        # a customer code ending in a digit (e.g. ``ACME2026``) ran
        # straight into the counter and read as one opaque token.
        number=f"{inv.series}-{n}",
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
    collegata = await _resolve_collegata(session, org_id=org_id, inv=inv)
    return _build_xml(
        inv,
        p.issuer,
        p.client,
        p.lines,
        "ANTEPRIMA",
        numero_override=p.number,
        collegata=collegata,
        intermediary=get_channel().intermediary,
    )


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
    # Pass the resolved would-be number (e.g. "CYLOCK2") so the PDF
    # shows what the document will become, with the state as a smaller
    # tag — a transmitted invoice prints the real number; a draft
    # prints the prospective number + a DRAFT/BOZZA marker. Surfacing
    # "BOZZA" alone hides the information the user actually needs
    # (which number am I about to emit?).
    is_draft = inv.state == InvoiceState.draft
    pdf = build_pdf(inv, p.issuer, p.client, p.lines, p.totals, number=p.number, is_draft=is_draft)
    return p.number, pdf
