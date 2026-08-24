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

import asyncio
import datetime as dt
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.config import get_settings
from mycelium_core.db import tenant_checkpoint, tenant_rollback
from mycelium_core.errors import ConflictError, DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.it_provinces import is_valid_provincia
from mycelium_core.models.client_profile import ClientProfile
from mycelium_core.models.invoice import (
    BuyerVerdict,
    ConservationAdhesion,
    ConservationStatus,
    DocumentType,
    Invoice,
    InvoiceCounter,
    InvoiceKind,
    InvoiceLine,
    InvoiceLineAltriDati,
    InvoiceState,
    IssuerProfile,
    PaymentStatus,
    SdiStatus,
    SdiTransmissionCounter,
)
from mycelium_core.models.membership import Role
from mycelium_core.models.sdi_notification import InvoiceNotification
from mycelium_core.sdi_channel import IntermediaryIdentity, SdiChannel, get_channel
from mycelium_core.services import audit
from mycelium_core.services import webhooks as webhooks_svc
from mycelium_core.services.image_validation import (
    IMAGE_MAX_BYTES,
    IMAGE_MIMES,
    image_is_decodable,
)

# Pure formatting / tax-math / XML helpers live in invoice_format
# (#54). Imported with their original names so every call site in this
# module is unchanged. ``BOLLO_DICITURA`` is re-exported because
# invoice_pdf imports it from here.
from mycelium_core.services.invoice_format import (
    FORFETTARIO_CAUSALE,
    Totals,
    _bare_id_codice,
    _build_xml,
    _compute_totals,
    _effective_iban,
    _is_forfettario,
    _resolve_line_tax,
    is_basic_latin,
    is_latin1,
)
from mycelium_core.services.invoice_xsd import validate_fatturapa
from mycelium_core.services.payment_methods import (
    validate_condizioni,
    validate_modalita,
    validate_terms_days,
)
from mycelium_core.services.rbac import require_role
from mycelium_core.services.sdi_mandate import get_active_mandate
from mycelium_core.services.sdi_transport import fatturapa_filename, transmission_progressivo
from mycelium_core.services.system_settings import (
    endpoint_for,
    get_sdi_environment,
)
from mycelium_core.vat import is_valid_vat_code, normalize_vat

log = logging.getLogger(__name__)

# --- issuer profiles (the invoice "intestazione") ---

_PROFILE_FIELDS = frozenset(
    {
        "label",
        "legal_name",
        "vat_number",
        "tax_code",
        "tax_regime",
        "country_code",
        "address",
        "civic_number",
        "postal_code",
        "city",
        "province",
        "country",
        "sdi_code",
        "rea",
        "default_iban",
        "legal_reference",
        "first_name",
        "last_name",
        "pec",
        "email",
        "phone",
        "fax",
        "show_phone",
        "show_email",
        "show_pec",
        "default_payment_conditions_code",
        "default_payment_method_code",
        "default_payment_terms_days",
        "letterhead",
        "logo_kind",
        "logo_position",
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
    legal_name: str | None = None,
    vat_number: str | None = None,
    tax_code: str | None = None,
    tax_regime: str = "RF01",
    country_code: str = "IT",
    address: str = "",
    civic_number: str | None = None,
    postal_code: str = "",
    city: str = "",
    province: str | None = None,
    country: str = "IT",
    sdi_code: str | None = None,
    rea: str | None = None,
    default_iban: str | None = None,
    legal_reference: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    pec: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    fax: str | None = None,
    show_phone: bool = True,
    show_email: bool = True,
    show_pec: bool = True,
    default_payment_conditions_code: str | None = None,
    default_payment_method_code: str | None = None,
    default_payment_terms_days: int | None = None,
    letterhead: str | None = None,
    is_default: bool = False,
) -> IssuerProfile:
    await require_role(session, org_id, actor_id, Role.admin)
    # Anagrafica choice: a profile must carry Denominazione (legal_name) OR
    # Nome+Cognome (both), never neither -- otherwise it can never emit a valid
    # FatturaPA Anagrafica.
    if not _valid_anagrafica(legal_name, first_name, last_name):
        raise DomainError(
            MessageCode.FISCAL_PROFILE_REQUIRED, detail="legal_name|first_name+last_name"
        )
    # The first profile is always the default; an explicit default
    # demotes the others (partial unique index: one default per org).
    existing = await list_issuer_profiles(session, org_id=org_id)
    make_default = is_default or not existing
    if make_default:
        await _clear_default(session)
    # Normalize a VIES-form P.IVA ("IT01112223334") into IdPaese + bare
    # IdCodice; reject a malformed code (FatturaPA IdCodice is the number
    # only, no country prefix).
    new_paese, vat_number = normalize_vat(vat_number, country_code)
    country_code = new_paese or country_code
    if not is_valid_vat_code(vat_number, country_code):
        raise DomainError(MessageCode.FISCAL_PROFILE_REQUIRED, detail=f"vat_number '{vat_number}'")
    # Closed enums (FatturaPA tables TPxx/MPxx) and a sane integer range
    # for the net-days default: catch typos here before they reach SdI.
    default_payment_conditions_code = validate_condizioni(default_payment_conditions_code)
    default_payment_method_code = validate_modalita(default_payment_method_code)
    default_payment_terms_days = validate_terms_days(default_payment_terms_days)
    postal_code = validate_issuer_cap(postal_code) or ""
    p = IssuerProfile(
        org_id=org_id,
        label=label,
        legal_name=legal_name,
        vat_number=vat_number,
        tax_code=tax_code,
        tax_regime=tax_regime,
        country_code=country_code,
        address=address,
        civic_number=civic_number,
        postal_code=postal_code,
        city=city,
        province=province,
        country=country,
        sdi_code=sdi_code,
        rea=rea,
        default_iban=default_iban,
        legal_reference=legal_reference,
        first_name=first_name,
        last_name=last_name,
        pec=pec,
        email=email,
        phone=phone,
        fax=fax,
        show_phone=show_phone,
        show_email=show_email,
        show_pec=show_pec,
        default_payment_conditions_code=default_payment_conditions_code,
        default_payment_method_code=default_payment_method_code,
        default_payment_terms_days=default_payment_terms_days,
        letterhead=letterhead,
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
    if "default_payment_conditions_code" in values:
        values["default_payment_conditions_code"] = validate_condizioni(
            values["default_payment_conditions_code"]  # type: ignore[arg-type]
        )
    if "default_payment_method_code" in values:
        values["default_payment_method_code"] = validate_modalita(
            values["default_payment_method_code"]  # type: ignore[arg-type]
        )
    if "default_payment_terms_days" in values:
        values["default_payment_terms_days"] = validate_terms_days(
            values["default_payment_terms_days"]  # type: ignore[arg-type]
        )
    if "postal_code" in values:
        values["postal_code"] = validate_issuer_cap(
            values["postal_code"]  # type: ignore[arg-type]
        )
    if "logo_kind" in values and values["logo_kind"] not in _LOGO_KINDS:
        raise DomainError(MessageCode.DOMAIN_ERROR, detail=f"logo_kind {values['logo_kind']!r}")
    if "logo_position" in values and values["logo_position"] not in _LOGO_POSITIONS:
        raise DomainError(
            MessageCode.DOMAIN_ERROR, detail=f"logo_position {values['logo_position']!r}"
        )
    for field, value in values.items():
        setattr(p, field, value)
    # Anagrafica invariant on the merged row: a patch that would clear the last
    # naming mode (e.g. blanking legal_name without Nome+Cognome) is rejected.
    if not _valid_anagrafica(p.legal_name, p.first_name, p.last_name):
        raise DomainError(
            MessageCode.FISCAL_PROFILE_REQUIRED, detail="legal_name|first_name+last_name"
        )
    if "vat_number" in values or "country_code" in values:
        new_paese, p.vat_number = normalize_vat(p.vat_number, p.country_code)
        if new_paese is not None:
            p.country_code = new_paese
        if not is_valid_vat_code(p.vat_number, p.country_code):
            raise DomainError(
                MessageCode.FISCAL_PROFILE_REQUIRED, detail=f"vat_number '{p.vat_number}'"
            )
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


# --- issuer logo (the courtesy-PDF letterhead image) ---

# A letterhead mark is small; cap it so a misplaced large file cannot
# bloat the row. PNG/JPEG only (reportlab raster formats); SVG would
# need an extra dependency.
# Re-exported under the historical names (the router reads svc.LOGO_MAX_BYTES);
# the canonical definitions live in the shared image_validation module.
LOGO_MAX_BYTES = IMAGE_MAX_BYTES
LOGO_MIMES = IMAGE_MIMES

# How a stored logo was produced, and where it prints relative to the
# letterhead title. Validated at the service boundary; the PDF falls back to
# sane defaults for anything else.
_LOGO_KINDS = frozenset({"image", "avatar", "avatar_qr"})
_LOGO_POSITIONS = frozenset({"left", "right", "top"})


async def set_issuer_logo(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    profile_id: uuid.UUID,
    data: bytes,
    mime: str,
    filename: str | None = None,
    kind: str = "image",
    qr_fields: str | None = None,
    qr_ecc: str | None = None,
) -> IssuerProfile:
    """Store/replace the issuer's letterhead logo. PNG/JPEG, size-capped.

    ``kind`` records how the image was produced (image | avatar | avatar_qr);
    it drives the courtesy-PDF logo box (a scannable QR gets a bigger square).
    For an ``avatar_qr`` logo, ``qr_fields`` (comma-separated vCard keys) and
    ``qr_ecc`` are persisted so the config card restores the exact selection.
    """
    await require_role(session, org_id, actor_id, Role.admin)
    if kind not in _LOGO_KINDS:
        raise DomainError(MessageCode.DOMAIN_ERROR, detail=f"logo kind '{kind}'")
    if mime not in LOGO_MIMES:
        raise DomainError(MessageCode.DOMAIN_ERROR, detail=f"logo mime '{mime}'")
    if not data:
        raise DomainError(MessageCode.DOMAIN_ERROR, detail="empty logo")
    if len(data) > LOGO_MAX_BYTES:
        raise DomainError(MessageCode.DOMAIN_ERROR, detail="logo too large")
    # Reject a corrupt / non-raster upload here (the declared mime is the
    # client's, never sniffed): storing one would 500 every later PDF
    # render for this issuer at draw time. Decode-validate before persist.
    if not image_is_decodable(data):
        raise DomainError(MessageCode.DOMAIN_ERROR, detail="logo not a decodable image")
    p = await get_issuer_profile(session, org_id=org_id, profile_id=profile_id)
    p.logo_data = data
    p.logo_mime = mime
    p.logo_kind = kind
    # Persist the QR recipe only for an avatar_qr logo, so switching to a plain
    # image/avatar upload does not wipe the saved selection.
    if kind == "avatar_qr":
        p.logo_qr_fields = (qr_fields or "")[:128]
        p.logo_qr_ecc = qr_ecc if qr_ecc in ("L", "M", "Q", "H") else "H"
    # logo_filename is VARCHAR(255); a longer client filename would raise a
    # DataError on flush. Truncate (display-only field).
    p.logo_filename = filename[:255] if filename else None
    p.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="issuer_profile",
        entity_id=p.id,
        action="set_logo",
    )
    return p


async def get_issuer_logo(
    session: AsyncSession, *, org_id: uuid.UUID, profile_id: uuid.UUID
) -> tuple[bytes, str] | None:
    """The logo bytes + mime, or None when unset. An explicit column-
    select: ``logo_data`` is a deferred ORM column, so the normal profile
    queries never pull it; only this path (and the PDF builder) loads it.
    RLS scopes the row to the tenant session."""
    row = (
        await session.execute(
            select(IssuerProfile.logo_data, IssuerProfile.logo_mime).where(
                IssuerProfile.id == profile_id
            )
        )
    ).one_or_none()
    if row is None or row[0] is None:
        return None
    return bytes(row[0]), (row[1] or "application/octet-stream")


async def clear_issuer_logo(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    profile_id: uuid.UUID,
) -> IssuerProfile:
    """Remove the issuer's letterhead logo."""
    await require_role(session, org_id, actor_id, Role.admin)
    p = await get_issuer_profile(session, org_id=org_id, profile_id=profile_id)
    p.logo_data = None
    p.logo_mime = None
    p.logo_filename = None
    p.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="issuer_profile",
        entity_id=p.id,
        action="clear_logo",
    )
    return p


async def set_conservation_adhesion(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    profile_id: uuid.UUID,
    adhesion: str,
) -> IssuerProfile:
    """Track the AdE free-conservation adhesion (ADR-0010), per issuer
    identity (it is per P.IVA); Mycelium guides it, it cannot adhere on the
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
    the tenant has already emitted N invoices elsewhere and wants Mycelium
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
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    invoice_id: uuid.UUID,
    include_deleted: bool = False,
    for_update: bool = False,
) -> Invoice:
    stmt = select(Invoice).where(Invoice.id == invoice_id)
    if for_update:
        # Serialise concurrent state transitions (transmit / reopen) on this
        # row: a second caller blocks until the first commits, then sees the
        # new state -- no double-transmit under two numbers, no reopen race.
        # populate_existing is load-bearing (ADR-0046): the invoice may
        # already sit in the session identity map from an unlocked read
        # (e.g. the public API's issuer scope check); without it the SELECT
        # FOR UPDATE would return the STALE cached object after waiting for
        # a concurrent transmit's pre-dispatch commit, and the lease gate
        # would never fire.
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    inv = (await session.execute(stmt)).scalar_one_or_none()
    # A trashed invoice is a 404 to every normal operation (it must be
    # restored first); the trash/restore path opts in via include_deleted.
    if inv is None or (inv.deleted_at is not None and not include_deleted):
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
    base = _derive_series_base(client.legal_name)
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
    purpose: str | None = None,
    issuer_profile_id: uuid.UUID | None = None,
    document_type: DocumentType = DocumentType.TD01,
    kind: InvoiceKind = InvoiceKind.invoice,
    parent_invoice_id: uuid.UUID | None = None,
    inherit_payment_defaults: bool = True,
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
    # Forfettario (RF19): default the mandatory L.190/2014 purpose when
    # the caller gave none (an explicit purpose is always honoured).
    purpose = validate_purpose(purpose)
    if purpose is None and _is_forfettario(issuer):
        purpose = FORFETTARIO_CAUSALE
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
        purpose=purpose,
    )
    session.add(inv)
    await session.flush()
    # Resolve and freeze the effective payment IBAN now so it is
    # visible/editable on the draft (precedence: invoice > client >
    # issuer). The client may not have a profile yet at draft time;
    # that is fine, update_draft re-resolves while still empty.
    # ``inherit_payment_defaults=False`` is how an automated composer says
    # "the payment story of this document is not the issuer's standing one".
    # A card connector must not inherit a bonifico IBAN: the IBAN alone opens
    # the DatiPagamento block in the serializer, and ModalitaPagamento then
    # falls through to the module default MP05, stating on a fiscal document
    # that a card charge was a bank transfer. Defaulting to True leaves every
    # hand-written invoice byte-for-byte as it was.
    if inherit_payment_defaults:
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
        "purpose",
        "notes",
        "payment_iban",
        "payment_due_date",
        "payment_conditions_code",
        "payment_method_code",
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
    inherit_payment_defaults: bool = True,
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
    if "series" in values and values["series"] != inv.series and inv.number is not None:
        # A draft that already carries an allocated number (a reopened scarto,
        # or a definite-fail revert) has its numero identity fixed under its
        # sezionale: moving series would re-use the number in another sequence.
        raise ConflictError(
            MessageCode.INVOICE_INVALID, detail="series locked after number allocation"
        )
    # Validate the closed-enum / range fields before we touch the row.
    if "payment_conditions_code" in values:
        values["payment_conditions_code"] = validate_condizioni(
            values["payment_conditions_code"]  # type: ignore[arg-type]
        )
    if "payment_method_code" in values:
        values["payment_method_code"] = validate_modalita(
            values["payment_method_code"]  # type: ignore[arg-type]
        )
    if "payment_terms_days" in values:
        values["payment_terms_days"] = validate_terms_days(
            values["payment_terms_days"]  # type: ignore[arg-type]
        )
    if "purpose" in values:
        values["purpose"] = validate_purpose(values["purpose"])  # type: ignore[arg-type]
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
    if inherit_payment_defaults and not inv.payment_iban:
        iban, _src = _effective_iban(inv, cp, issuer)
        if iban is not None:
            inv.payment_iban = iban
    # When the user set net-days but no explicit due date, materialize
    # the due date now so the draft preview shows it. The resolver picks
    # up the days from the client / issuer too; auto-fill only when the
    # field is empty (an explicit user-set date is never overwritten).
    if inherit_payment_defaults and inv.payment_due_date is None:
        from mycelium_core.services.payment_methods import resolve_payment as _rp

        resolved = _rp(inv, cp, issuer)
        if resolved.terms_days is not None:
            base = (inv.issued_at or dt.datetime.now(tz=dt.UTC)).date()
            inv.payment_due_date = base + dt.timedelta(days=resolved.terms_days)
    # The issuer (hence regime, stamp_duty and forfettario-ness) may have
    # changed: keep taxable/vat/stamp_duty/total consistent.
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
    vat_nature: str | None = None,
    altri_dati: Sequence[AltriDatiBlock] | None = None,
) -> InvoiceLine:
    inv = await get_invoice(session, org_id=org_id, invoice_id=invoice_id)
    _require_draft(inv)
    await require_role(session, org_id, actor_id, Role.member)
    # Validated before the line row exists: a malformed block must not
    # leave a half-created line behind (FatturaPA 2.2.1.16).
    validated = _validate_altri_dati(altri_dati or ())
    issuer = await _resolve_issuer(session, org_id=org_id, inv=inv)
    vat_rate, vat_nature = _resolve_line_tax(issuer, vat_rate, vat_nature)
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
        vat_nature=vat_nature,
    )
    session.add(line)
    await session.flush()
    if altri_dati is not None:
        await _write_altri_dati(session, org_id=org_id, line_id=line.id, blocks=validated)
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
    vat_nature: str | None = None,
    altri_dati: Sequence[AltriDatiBlock] | None = None,
) -> InvoiceLine:
    """Edit a draft line. ``altri_dati`` is tri-state on purpose: None
    leaves the line's AltriDatiGestionali untouched (so a caller that
    only fixes a price cannot silently drop them), while an explicit
    sequence REPLACES the set -- ``[]`` clears it."""
    inv = await get_invoice(session, org_id=org_id, invoice_id=invoice_id)
    _require_draft(inv)
    await require_role(session, org_id, actor_id, Role.member)
    validated = _validate_altri_dati(altri_dati or ())
    line = await get_line(session, org_id=org_id, invoice_id=invoice_id, line_id=line_id)
    issuer = await _resolve_issuer(session, org_id=org_id, inv=inv)
    vat_rate, vat_nature = _resolve_line_tax(issuer, vat_rate, vat_nature)
    line.description = description
    line.unit_price = unit_price
    line.quantity = quantity
    line.vat_rate = vat_rate
    line.vat_nature = vat_nature
    await session.flush()
    if altri_dati is not None:
        await _write_altri_dati(session, org_id=org_id, line_id=line.id, blocks=validated)
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
    # Presence check only: the line's AltriDatiGestionali rows go with it
    # (FK ON DELETE CASCADE, migration 0088).
    await get_line(session, org_id=org_id, invoice_id=invoice_id, line_id=line_id)
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


# --- AltriDatiGestionali (FatturaPA 2.2.1.16, migration 0088) ---

# Facets read from the shipped XSD (services/fatturapa_xsd/
# Schema_VFPA12_V1.2.3.xsd), not from prose:
#   TipoDato          String10Type       (\p{IsBasicLatin}{1,10})
#   RiferimentoTesto  String60LatinType  ([IsBasicLatin|IsLatin-1Supplement]{1,60})
#   RiferimentoNumero Amount8DecimalType ([\-]?[0-9]{1,11}\.[0-9]{2,8})
#   RiferimentoData   xs:date
# We reject here rather than at the XSD gate so the caller gets a coded
# DomainError on ITS field instead of a wall of schema violations, and
# so a non-conformant block can never reach the frozen XML.
_TIPO_DATO_MAX = 10
_RIF_TESTO_MAX = 60
_RIF_NUMERO_MAX_DECIMALS = 8
_RIF_NUMERO_LIMIT = Decimal(10) ** 11  # 11 integer digits
# maxOccurs is "unbounded" in the XSD; an HTTP body must not be. The cap
# is a sanity bound on one line's set (the real ones run to 1-3 blocks),
# not a fiscal rule.
_ALTRI_DATI_MAX_BLOCKS = 50


@dataclass(frozen=True)
class AltriDatiBlock:
    """One AltriDatiGestionali block as the caller supplies it.

    ``tipo_dato`` is a LABEL naming the kind of data (INTENTO,
    N.DOC.COMM, NB3, ...) and is the only required field; the free text
    goes in ``riferimento_testo``. The spec fixes no enum, so no
    whitelist is applied. Empty by default: a line with no block emits
    nothing."""

    tipo_dato: str
    riferimento_testo: str | None = None
    riferimento_numero: Decimal | None = None
    riferimento_data: dt.date | None = None


#: ``invoices.purpose`` is varchar(200), and it is also FatturaPA
#: String200LatinType. One bound, two reasons to hold it.
_PURPOSE_MAX = 200


def validate_purpose(value: str | None) -> str | None:
    """Bound the causale where the domain accepts it, not at each caller.

    Refused rather than truncated: a causale cut mid-sentence sits on a document
    kept for ten years, and the caller who supplied it is a person or a tool
    that can be told. The connector mappers clamp BEFORE they get here, on
    purpose and for the opposite reason -- there the alternative is an event
    that retries a payload which can never succeed (see
    ``payment_events._FIELD_LIMITS``).

    The API bounds it too, at 200, so an HTTP caller still gets a 422 rather
    than this. What this closes is every other door: the MCP credit-note tool
    took an unbounded ``purpose``, and a long one raised a driver-level
    truncation error, which is not a DomainError and therefore reached the
    caller as an internal fault.
    """
    if value is None:
        return None
    if len(value) > _PURPOSE_MAX:
        raise DomainError(
            MessageCode.INVOICE_INVALID,
            detail=f"purpose: at most {_PURPOSE_MAX} characters ({len(value)} given)",
        )
    return value


def validate_issuer_cap(value: str | None) -> str | None:
    """The issuer's CAP against the FatturaPA facet, at the write boundary.

    ``CAPType`` is ``[0-9]{5}`` and 1.2.2.3 <CAP> is <1.1> in the AdE tracciato:
    mandatory, exactly five digits, no exceptions. Refused HERE rather than at
    the XSD gate so the person editing the profile gets an error on the field
    they are editing, instead of every future invoice sticking in draft with a
    schema message. That is not hypothetical: a six-digit CAP (a real 20129 with
    an extra digit) sat on a live profile and blocked emission with nothing
    pointing at the field.

    The issuer of an Italian electronic invoice is an Italian fiscal subject by
    construction, so there is no foreign case to admit here. Deliberately NOT
    applied to the counterpart: a foreign client's postal code genuinely is not
    a five-digit CAP, and this session found no AdE rule prescribing what to put
    in its place, so inventing one would be inventing fiscal data. That case
    stays with the schema gate, which now runs on the preview too.
    """
    if value is None:
        return None
    text = value.strip()
    if text and not re.fullmatch(r"[0-9]{5}", text):
        raise DomainError(
            MessageCode.INVOICE_INVALID, detail=f"postal_code '{text}': CAP is 5 digits"
        )
    return text


def _validate_altri_dati(blocks: Sequence[AltriDatiBlock]) -> list[AltriDatiBlock]:
    """Normalise + validate one line's blocks against the XSD facets,
    returning the values to persist. Called BEFORE any write so an
    invalid block never leaves a half-written line behind.

    Normalisation is deliberately narrow: surrounding whitespace is
    stripped (xs:normalizedString semantics) and a text that reduces to
    empty becomes NULL, i.e. the element is simply not emitted -- '' is
    not a valid String60LatinType and is refused by
    ck_invoice_line_altri_dati_riferimento_testo_len (migration 0088)."""
    if len(blocks) > _ALTRI_DATI_MAX_BLOCKS:
        raise DomainError(
            MessageCode.INVOICE_ALTRI_DATI_INVALID,
            detail=f"at most {_ALTRI_DATI_MAX_BLOCKS} blocks per line",
        )
    out: list[AltriDatiBlock] = []
    for b in blocks:
        tipo = (b.tipo_dato or "").strip()
        if not tipo or len(tipo) > _TIPO_DATO_MAX:
            raise DomainError(
                MessageCode.INVOICE_ALTRI_DATI_INVALID,
                detail=f"tipo_dato: required, 1..{_TIPO_DATO_MAX} characters",
            )
        if not is_basic_latin(tipo):
            raise DomainError(
                MessageCode.INVOICE_ALTRI_DATI_INVALID,
                detail="tipo_dato: Basic-Latin characters only",
            )
        testo = (b.riferimento_testo or "").strip() or None
        if testo is not None:
            if len(testo) > _RIF_TESTO_MAX:
                raise DomainError(
                    MessageCode.INVOICE_ALTRI_DATI_INVALID,
                    detail=f"riferimento_testo: at most {_RIF_TESTO_MAX} characters",
                )
            if not is_latin1(testo):
                raise DomainError(
                    MessageCode.INVOICE_ALTRI_DATI_INVALID,
                    detail="riferimento_testo: Latin-1 characters only",
                )
        numero = b.riferimento_numero
        if numero is not None:
            if not numero.is_finite():
                raise DomainError(
                    MessageCode.INVOICE_ALTRI_DATI_INVALID,
                    detail="riferimento_numero: not a finite decimal",
                )
            if abs(numero) >= _RIF_NUMERO_LIMIT:
                raise DomainError(
                    MessageCode.INVOICE_ALTRI_DATI_INVALID,
                    detail="riferimento_numero: at most 11 integer digits",
                )
            # ``is_finite`` above guarantees an int exponent (a NaN/Inf
            # carries 'n'/'F'); the isinstance keeps that provable for
            # the type checker. Beyond 8 decimals the value is outside
            # Amount8DecimalType AND numeric(21,8) would silently round
            # it, so it is refused rather than quietly truncated.
            exponent = numero.as_tuple().exponent
            if isinstance(exponent, int) and exponent < -_RIF_NUMERO_MAX_DECIMALS:
                raise DomainError(
                    MessageCode.INVOICE_ALTRI_DATI_INVALID,
                    detail=f"riferimento_numero: at most {_RIF_NUMERO_MAX_DECIMALS} decimals",
                )
        data = b.riferimento_data
        if isinstance(data, dt.datetime):
            # xs:date, not xs:dateTime: keep only the calendar day.
            data = data.date()
        if data is not None and not isinstance(data, dt.date):
            raise DomainError(
                MessageCode.INVOICE_ALTRI_DATI_INVALID,
                detail="riferimento_data: not a date",
            )
        out.append(
            AltriDatiBlock(
                tipo_dato=tipo,
                riferimento_testo=testo,
                riferimento_numero=numero,
                riferimento_data=data,
            )
        )
    return out


async def get_line(
    session: AsyncSession, *, org_id: uuid.UUID, invoice_id: uuid.UUID, line_id: uuid.UUID
) -> InvoiceLine:
    """The line, scoped to its invoice (and to the org by RLS, like
    ``list_lines``: ``org_id`` is carried for signature consistency, the
    tenant fence is the policy, not this predicate)."""
    line = (
        await session.execute(
            select(InvoiceLine).where(
                InvoiceLine.id == line_id, InvoiceLine.invoice_id == invoice_id
            )
        )
    ).scalar_one_or_none()
    if line is None:
        raise NotFoundError(MessageCode.INVOICE_NOT_FOUND, detail="line")
    return line


async def _write_altri_dati(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    line_id: uuid.UUID,
    blocks: Sequence[AltriDatiBlock],
) -> None:
    """REPLACE the line's blocks with ``blocks`` (already validated).

    Replace, not per-block CRUD: the set is ordered and small and the UI
    edits it as a whole, so ``ord`` is simply re-assigned 1..n from the
    caller's order. Delete-then-insert also keeps the rewrite free of
    transient uq_invoice_line_altri_dati_ord violations."""
    await session.execute(
        delete(InvoiceLineAltriDati).where(InvoiceLineAltriDati.invoice_line_id == line_id)
    )
    for ord_, b in enumerate(blocks, start=1):
        session.add(
            InvoiceLineAltriDati(
                org_id=org_id,
                invoice_line_id=line_id,
                ord=ord_,
                tipo_dato=b.tipo_dato,
                riferimento_testo=b.riferimento_testo,
                riferimento_numero=b.riferimento_numero,
                riferimento_data=b.riferimento_data,
            )
        )
    await session.flush()


async def list_line_altri_dati(
    session: AsyncSession, *, org_id: uuid.UUID, line_id: uuid.UUID
) -> list[InvoiceLineAltriDati]:
    """One line's blocks in emission order. Readable in any state: the
    blocks of a transmitted invoice are the ones its frozen XML carries."""
    return list(
        (
            await session.execute(
                select(InvoiceLineAltriDati)
                .where(InvoiceLineAltriDati.invoice_line_id == line_id)
                .order_by(InvoiceLineAltriDati.ord)
            )
        )
        .scalars()
        .all()
    )


async def list_invoice_altri_dati(
    session: AsyncSession, *, org_id: uuid.UUID, invoice_id: uuid.UUID
) -> dict[uuid.UUID, list[InvoiceLineAltriDati]]:
    """Every line's blocks for one invoice, keyed by line id, in emission
    order. One query instead of one-per-line: the XML/PDF builders and
    the line listing all need the whole set at once. Lines with no block
    are simply absent from the mapping (empty is the normal case)."""
    rows = (
        (
            await session.execute(
                select(InvoiceLineAltriDati)
                .join(InvoiceLine, InvoiceLine.id == InvoiceLineAltriDati.invoice_line_id)
                .where(InvoiceLine.invoice_id == invoice_id)
                .order_by(InvoiceLine.line_no, InvoiceLineAltriDati.ord)
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[uuid.UUID, list[InvoiceLineAltriDati]] = {}
    for r in rows:
        grouped.setdefault(r.invoice_line_id, []).append(r)
    return grouped


async def replace_line_altri_dati(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    invoice_id: uuid.UUID,
    line_id: uuid.UUID,
    blocks: Sequence[AltriDatiBlock],
) -> list[InvoiceLineAltriDati]:
    """Set a line's AltriDatiGestionali to exactly ``blocks`` (an empty
    sequence clears them). Draft-only, like every other line edit: after
    emission the document is append-only (ADR-0009) and its XML is frozen
    (ADR-0046), so a later change here could not reach what was sent."""
    inv = await get_invoice(session, org_id=org_id, invoice_id=invoice_id)
    _require_draft(inv)
    await require_role(session, org_id, actor_id, Role.member)
    validated = _validate_altri_dati(blocks)
    line = await get_line(session, org_id=org_id, invoice_id=invoice_id, line_id=line_id)
    await _write_altri_dati(session, org_id=org_id, line_id=line.id, blocks=validated)
    return await list_line_altri_dati(session, org_id=org_id, line_id=line.id)


async def delete_draft(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    invoice_id: uuid.UUID,
) -> None:
    # Permanent (hard) delete, only for drafts. Reachable on a trashed
    # draft too (the "delete permanently" action in the recycle bin), so
    # include_deleted is set; a transmitted invoice can never be purged.
    inv = await get_invoice(session, org_id=org_id, invoice_id=invoice_id, include_deleted=True)
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


# Accreditation / SDICoop-interoperability test invoices: the test suite
# emits them with the "TEST" sezionale and a fixed purpose. They are
# transmissions to the SdI *test* host, not fiscal documents, so the
# draft-only deletion guard (which protects real emitted invoices) does
# not apply. One-off cleanup so the accreditation noise leaves the list.
TEST_SERIES = "TEST"
TEST_PURPOSE_PREFIX = "Prestazione di test interoperabilita"


async def delete_test_invoices(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID
) -> int:
    """Hard-delete this org's accreditation test invoices (series "TEST"
    + the fixed test purpose) regardless of state, returning the count.
    Idempotent: a no-op when none remain."""
    await require_role(session, org_id, actor_id, Role.admin)
    ids = list(
        (
            await session.execute(
                select(Invoice.id).where(
                    Invoice.series == TEST_SERIES,
                    Invoice.purpose.like(f"{TEST_PURPOSE_PREFIX}%"),
                )
            )
        ).scalars()
    )
    if not ids:
        return 0
    # invoice_notifications FK is ON DELETE RESTRICT -> remove the audit
    # rows first; invoice_lines cascade with the invoice row.
    await session.execute(
        delete(InvoiceNotification).where(InvoiceNotification.invoice_id.in_(ids))
    )
    await session.execute(delete(Invoice).where(Invoice.id.in_(ids)))
    # Drop the now-empty TEST counter rows so the test sezionale does not
    # linger (numbering is per issuer+series+year; RLS scopes to this org).
    await session.execute(delete(InvoiceCounter).where(InvoiceCounter.series == TEST_SERIES))
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="invoice",
        entity_id=None,
        action="purge_test_invoices",
        diff={"deleted": len(ids)},
    )
    return len(ids)


async def list_invoice_notifications(
    session: AsyncSession, *, org_id: uuid.UUID, invoice_id: uuid.UUID
) -> list[InvoiceNotification]:
    """Every SdI notification recorded against this invoice, oldest first
    (the transmission timeline: RC/MC/NS/AT/NE/DT). Validates the invoice
    is in the tenant first (404 otherwise)."""
    await get_invoice(session, org_id=org_id, invoice_id=invoice_id)
    return list(
        (
            await session.execute(
                select(InvoiceNotification)
                .where(InvoiceNotification.invoice_id == invoice_id)
                .order_by(InvoiceNotification.received_at.asc())
            )
        )
        .scalars()
        .all()
    )


async def get_invoice_notification_xml(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    invoice_id: uuid.UUID,
    notification_id: uuid.UUID,
) -> tuple[str, str | None]:
    """The raw signed SdI notification XML (RC/MC/NS/AT/NE/DT) for view or
    download: the XAdES-signed document SdI delivered -- the legal proof of the
    transmission outcome. Scoped to the invoice + tenant (404 if the invoice is
    not the tenant's or no such notification hangs off it). ``raw_xml`` decodes
    as UTF-8 (SdI notifiche are enveloped-signature XML, not a p7m envelope)."""
    await get_invoice(session, org_id=org_id, invoice_id=invoice_id)
    n = (
        await session.execute(
            select(InvoiceNotification).where(
                InvoiceNotification.id == notification_id,
                InvoiceNotification.invoice_id == invoice_id,
            )
        )
    ).scalar_one_or_none()
    if n is None:
        raise NotFoundError(MessageCode.INVOICE_NOT_FOUND)
    return bytes(n.raw_xml).decode("utf-8", "replace"), n.file_name


async def _resolve_issuer(
    session: AsyncSession, *, org_id: uuid.UUID, inv: Invoice
) -> IssuerProfile | None:
    """The invoice's issuer identity: the explicitly chosen profile, or
    the org default when none was picked."""
    if inv.issuer_profile_id is not None:
        return await get_issuer_profile(session, org_id=org_id, profile_id=inv.issuer_profile_id)
    return await get_default_issuer_profile(session, org_id=org_id)


async def _persist_totals(session: AsyncSession, *, org_id: uuid.UUID, inv: Invoice) -> Totals:
    """Recompute and store taxable/vat/stamp_duty/total on the draft so they
    stay consistent with the lines and the issuer's regime. Called from
    every mutation that changes lines or the issuer."""
    issuer = await _resolve_issuer(session, org_id=org_id, inv=inv)
    lines = await list_lines(session, org_id=org_id, invoice_id=inv.id)
    totals = _compute_totals(lines, issuer)
    inv.taxable = totals.taxable
    inv.vat = totals.vat
    inv.stamp_duty = totals.stamp_duty
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


def _valid_anagrafica(
    legal_name: str | None, first_name: str | None, last_name: str | None
) -> bool:
    """FatturaPA Anagrafica is a choice: exactly one naming mode must be
    complete -- ``Denominazione`` (legal_name) OR ``Nome``+``Cognome`` (both).
    Neither-set yields an empty/partial Anagrafica that SdI scarta, so the
    write paths and the transmit gate enforce this."""
    return bool(legal_name) or bool(first_name and last_name)


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
            ("address", fiscal.address),
            ("postal_code", fiscal.postal_code),
            ("city", fiscal.city),
        )
        if not v
    ]
    # Anagrafica: Denominazione (legal_name) OR Nome+Cognome (persona fisica).
    if not _valid_anagrafica(fiscal.legal_name, fiscal.first_name, fiscal.last_name):
        missing.append("legal_name|first_name+last_name")
    # The cedente's IdFiscaleIVA (P.IVA) is mandatory in FatturaPA (the
    # CodiceFiscale alone is not enough for the issuer; the cessionario may
    # have just a CodiceFiscale, the cedente may not).
    if not fiscal.vat_number:
        missing.append("vat_number")
    if missing:
        raise DomainError(MessageCode.FISCAL_PROFILE_REQUIRED, detail=", ".join(missing))
    # The cessionario's Sede (Indirizzo/CAP/Comune) is mandatory in
    # FatturaPA; surface a clean domain error here rather than letting the
    # XSD reject an empty Indirizzo with a cryptic pattern message.
    client_missing = [
        f
        for f, v in (
            ("legal_name", client.legal_name),
            ("address", client.address),
            ("postal_code", client.postal_code),
            ("city", client.city),
        )
        if not v
    ]
    if not (client.vat_number or client.tax_code):
        client_missing.append("vat_number|tax_code")
    if not (client.sdi_code or client.pec):
        client_missing.append("sdi_code|pec")
    if client_missing:
        raise DomainError(
            MessageCode.INVOICE_INVALID, detail="client: " + ", ".join(client_missing)
        )
    # Provincia: the XSD only checks the [A-Z]{2} shape; reject a well-formed
    # but nonexistent Italian province before SdI does (scarto).
    if not is_valid_provincia(fiscal.province, fiscal.country):
        raise DomainError(
            MessageCode.INVOICE_INVALID, detail=f"issuer province '{fiscal.province}'"
        )
    if not is_valid_provincia(client.province, client.country):
        raise DomainError(
            MessageCode.INVOICE_INVALID, detail=f"client province '{client.province}'"
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


def _payload_intermediary(
    fiscal: IssuerProfile, intermediary: IntermediaryIdentity | None
) -> IntermediaryIdentity | None:
    """Which identity goes in 1.1.1 ``IdTrasmittente``: the channel's, or None
    meaning the cedente's own.

    Self-transmission (the accredited-channel holder sending its OWN invoice,
    cedente VAT == channel id) returns None, and the cedente becomes its own
    trasmittente. That is not cosmetic: the None branch in ``_build_xml`` emits
    the cedente's CODICE FISCALE, which is what SdI validates IdTrasmittente
    against (a P.IVA there is scartata 00300, and for a physical-person channel
    holder the two differ). So this helper is a trasmittente selector and must
    not be mistaken for scaffolding of the 1.5/1.6 emitter block, which no
    longer exists (ADR-0053). Shared by ``transmit`` and ``get_xml_preview`` so
    the downloadable ANTEPRIMA is byte-faithful to the document emitted."""
    if intermediary is None:
        return None
    cedente_vat = (
        _bare_id_codice(fiscal.vat_number, fiscal.country_code) if fiscal.vat_number else None
    )
    return None if cedente_vat == intermediary.vat_number else intermediary


def _require_transmittable(inv: Invoice) -> bool:
    """Transmit gate (ADR-0046). Admits a draft (first attempt) or a RETRY of
    a dispatch that never produced an outcome: state=transmitted with a NULL
    ``identificativo_sdi`` and an EXPIRED dispatch lease. The lease is the
    single in-flight arbiter: fresh -> a concurrent/unsettled attempt owns the
    invoice (409); cleared -> the attempt settled (a manual-export transmit
    also ends ident-less but with the lease cleared, so it is NOT retryable).
    Returns True on the retry leg."""
    # A shadow document is not transmittable BY ANYONE, checked here rather than
    # in each caller. Shadow mode's promise is "nothing is sent", and the three
    # guards that keep the connector from filing are in the connector; this is
    # the single function that actually files, so a document composed while
    # shadowing is refused whatever route reaches it -- the SPA, the public
    # issuer-key API, a future caller nobody has written yet. ``promote_dry_run``
    # is the one way out, and it is deliberately an operator's decision.
    if inv.dry_run:
        raise ConflictError(MessageCode.INVOICE_DRY_RUN_NOT_SENDABLE)
    if inv.state is InvoiceState.draft:
        return False
    if inv.state is InvoiceState.transmitted and inv.identificativo_sdi is None:
        lease = inv.sdi_dispatch_started_at
        if lease is None:
            raise ConflictError(MessageCode.INVOICE_NOT_DRAFT)
        age = dt.datetime.now(tz=dt.UTC) - lease
        if age < dt.timedelta(seconds=get_settings().sdi_dispatch_lease_seconds):
            raise ConflictError(MessageCode.INVOICE_TRANSMIT_IN_PROGRESS)
        return True
    raise ConflictError(MessageCode.INVOICE_NOT_DRAFT)


def _dispatch_definitely_not_filed(exc: BaseException) -> bool:
    """True when the failed dispatch PROVABLY left nothing at SdI, so the
    invoice may return to draft. Only two shapes qualify: the connection was
    never established (no request bytes sent), or RiceviFile answered with an
    explicit ``Errore`` (received but refused). Everything else -- timeouts
    after connect, send/read errors, HTTP 5xx, a malformed response -- is
    AMBIGUOUS: SdI may hold the file, so the identifiers stay burned and a
    retry re-sends the SAME NomeFile (SdI dedupes by file name)."""
    from mycelium_core.services.sdi_transport import SdiFileRejectedError, SdiLocalConfigError

    if isinstance(exc, SdiFileRejectedError | SdiLocalConfigError):
        return True
    return isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)


async def _record_dispatch_failure(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    invoice_id: uuid.UUID,
    exc: BaseException,
    retrying: bool,
) -> None:
    """Phase 3, failure leg (ADR-0046). Runs AFTER the pre-dispatch commit,
    so the request transaction that is about to roll back (the caller
    re-raises) must not take this record with it: the leg commits itself via
    ``tenant_checkpoint``.

    Definitely-not-filed AND the attempt started from draft -> back to
    draft, KEEPING number/issued_at/progressivo/nome_file for verbatim reuse
    (nothing is at SdI; the fresh XML of the next attempt sails under the
    same name). On a RETRY the definite verdict only covers THIS attempt --
    the earlier lost-ACK one may have filed -- so the invoice stays parked
    (transmitted, ident-less, frozen XML kept). Ambiguous -> parked, lease
    untouched: retryable the moment the lease expires, resending the same
    frozen file.

    The row is re-read fresh (populate_existing; include_deleted, because a
    concurrent trash in the unlocked dispatch window must not lose the
    record of a dispatched file) and the leg no-ops if the inbound reconcile
    settled the invoice while the dispatch was in flight (a late RC adopting
    the ident): a settled fiscal document is never reverted."""
    inv = await get_invoice(
        session, org_id=org_id, invoice_id=invoice_id, for_update=True, include_deleted=True
    )
    if not (inv.state is InvoiceState.transmitted and inv.identificativo_sdi is None):
        # Settled while dispatching (inbound reconcile adopted the ident):
        # a settled document is never reverted; just drop the stale lease.
        if inv.sdi_dispatch_started_at is not None:
            inv.sdi_dispatch_started_at = None
            await session.flush()
            await tenant_checkpoint(session)
        return
    if _dispatch_definitely_not_filed(exc) and not retrying:
        inv.state = InvoiceState.draft
        inv.xml = None
        inv.sdi_dispatch_started_at = None
        action = "transmit_failed"
    else:
        action = "transmit_unconfirmed"
    inv.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="invoice",
        entity_id=inv.id,
        action=action,
        diff={"error": f"{type(exc).__name__}: {exc}"[:300], "nome_file": inv.nome_file},
    )
    await tenant_checkpoint(session)


async def transmit(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    invoice_id: uuid.UUID,
    progressivo: str | None = None,
    channel: SdiChannel | None = None,
) -> Invoice:
    """Two-phase durable transmit (ADR-0046).

    Phase 1 (prepare) locks the row, validates, allocates the fiscal
    identifiers (numero, ProgressivoInvio, NomeFile), freezes the XML, stamps
    the dispatch lease, and COMMITS via ``tenant_checkpoint``: everything a
    file at SdI could be identified by is durable BEFORE a byte leaves, and
    the counters can never hand the same identity to another document.
    Phase 2 dispatches with no lock or transaction held. Phase 3 records the
    outcome: success rides the caller's transaction (atomic with the public
    API's idempotency snapshot); failure self-commits via
    ``_record_dispatch_failure`` and re-raises.
    """
    # PHASE 1 -- prepare. The row lock serialises concurrent transmits until
    # the pre-dispatch commit; from then on the dispatch lease arbitrates.
    inv = await get_invoice(session, org_id=org_id, invoice_id=invoice_id, for_update=True)
    retrying = _require_transmittable(inv)
    await require_role(session, org_id, actor_id, Role.member)
    # Pick the RiceviFile endpoint at runtime from the DB switch (test vs
    # production), so the environment flips from Settings without a redeploy.
    sdi_env = await get_sdi_environment(session)
    if retrying and inv.sdi_env_used is not None and inv.sdi_env_used != sdi_env:
        # The NomeFile dedupe safety net holds only within ONE environment: a
        # retry after an environment flip could file the same name/bytes into
        # an environment that never saw the lost attempt, with the two
        # environments' notifications then cross-contaminating correlation.
        raise ConflictError(
            MessageCode.INVOICE_TRANSMIT_ENV_CHANGED,
            detail=f"{inv.sdi_env_used} -> {sdi_env}",
        )
    ch = channel or get_channel(endpoint_override=endpoint_for(sdi_env))
    intermediary = ch.intermediary
    if intermediary is not None:
        # Transmitting via the accredited channel = Mycelium acts as intermediary
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
    if retrying and inv.xml is not None:
        # Retry of an unsettled dispatch: the FROZEN document is the fiscal
        # source of truth (the lost attempt may have filed exactly these
        # bytes), so it is re-sent BYTE-IDENTICAL under the same NomeFile --
        # SdI dedupes by file name, so a landed original makes the resend
        # collide instead of double-filing. No live re-validation, no totals
        # recompute: the live issuer/client cards may have changed since the
        # freeze and must not be able to block or diverge from the resend.
        assert inv.progressivo_invio is not None and inv.nome_file is not None  # noqa: S101
        progressivo_str = inv.progressivo_invio
        filename = inv.nome_file
        xml = inv.xml
        # Persisted proof that a resend happened: the duplicate-echo guard on
        # a same-ident NS 00002 is only sound when we really re-sent the name.
        inv.sdi_resent_at = dt.datetime.now(tz=dt.UTC)
    else:
        # The chosen issuer identity (default if none); its header is frozen
        # into inv.xml below, so later edits never touch this document.
        fiscal = await _resolve_issuer(session, org_id=org_id, inv=inv)
        client = await _client(session, inv.client_tag_id)
        lines = await list_lines(session, org_id=org_id, invoice_id=invoice_id)
        # The lines' AltriDatiGestionali (FatturaPA 2.2.1.16), loaded with
        # the lines so they are frozen into inv.xml below together with
        # them: a block that is not read here would never reach SdI.
        line_altri_dati = await list_invoice_altri_dati(
            session, org_id=org_id, invoice_id=invoice_id
        )
        _validate(fiscal, client, lines)
        assert fiscal is not None  # _validate raised otherwise  # noqa: S101
        # Who goes in IdTrasmittente (the cedente itself for self-transmission,
        # else the channel holder) is resolved by the shared helper so the
        # downloadable ANTEPRIMA preview is byte-faithful to what we actually
        # emit here. The channel-level ``intermediary`` above still drives the
        # mandate requirement and the trasmittente sequence (ADR-0011); the
        # document body carries no emitter block either way (ADR-0053).
        payload_intermediary = _payload_intermediary(fiscal, intermediary)
        totals = _compute_totals(lines, fiscal)
        inv.taxable, inv.vat, inv.stamp_duty, inv.total = (
            totals.taxable,
            totals.vat,
            totals.stamp_duty,
            totals.total,
        )
        # A reopened scarto (rejected -> draft via reopen_rejected) keeps its
        # already-allocated number and original date: FatturaPA treats a
        # scartato document as never issued, so the correction is re-sent
        # within 5 days under the SAME numero + data -- allocating a new
        # number would leave a gap. A fresh draft (number/issued_at still
        # None) allocates and stamps now.
        if inv.number is None:
            inv.number = await _allocate_number(
                session,
                org_id=org_id,
                issuer_profile_id=fiscal.id,
                series=inv.series,
                year=inv.year,
            )
        if inv.issued_at is None:
            inv.issued_at = dt.datetime.now(tz=dt.UTC)
        # The SdI file-name progressivo is max 5 alphanumeric chars (Specifiche
        # SDI, Allegato B). It must also be unique per trasmittente across every
        # file ever sent, which the per-org/per-issuer invoice number is NOT
        # (it resets each year and is scoped differently). So both paths draw a
        # dedicated monotonic per-trasmittente sequence rendered base36 width 5
        # (``transmission_progressivo``); the trasmittente is the accredited
        # channel holder when Mycelium acts as intermediary, else the cedente
        # itself.
        if inv.progressivo_invio is not None and inv.nome_file is not None:
            # F8 durability: a prior attempt already allocated this document's
            # ProgressivoInvio / NomeFile (e.g. a definite-fail revert kept
            # them on the draft) -- reuse them verbatim so a resend collides
            # with SdI's own file-name dedupe rather than double-filing under
            # a fresh name. (reopen_rejected clears them, so a scarto still
            # gets a new one.)
            progressivo_str = inv.progressivo_invio
            filename = inv.nome_file
        elif intermediary is not None:
            seq = await _allocate_transmission_seq(session, intermediary_id=intermediary.vat_number)
            progressivo_str = transmission_progressivo(seq)
            filename = fatturapa_filename(
                intermediary.country_code, intermediary.vat_number, progressivo_str
            )
        else:
            cedente_id = fiscal.vat_number or fiscal.tax_code or ""
            if progressivo is not None:
                progressivo_str = progressivo
            else:
                seq = await _allocate_transmission_seq(session, intermediary_id=cedente_id)
                progressivo_str = transmission_progressivo(seq)
            filename = fatturapa_filename(fiscal.country_code, cedente_id, progressivo_str)
        # Record the emitted file identity on the invoice (audit + retry reuse).
        inv.progressivo_invio = progressivo_str
        inv.nome_file = filename
        collegata = await _resolve_collegata(session, org_id=org_id, inv=inv)
        xml = _build_xml(
            inv,
            fiscal,
            client,
            lines,
            progressivo_str,
            collegata=collegata,
            intermediary=payload_intermediary,
            altri_dati=line_altri_dati,
        )
        # Validate against the official FatturaPA XSD before emission: SdI
        # scarta anything non-conformant, so an invalid document must never
        # leave draft. Surfaced as the domain error the UI already renders.
        xsd_errors = validate_fatturapa(xml)
        if xsd_errors:
            raise DomainError(MessageCode.INVOICE_INVALID, detail="; ".join(xsd_errors[:5]))
        inv.xml = xml
    number = inv.number
    inv.state = InvoiceState.transmitted
    inv.sdi_dispatch_started_at = dt.datetime.now(tz=dt.UTC)
    inv.sdi_env_used = sdi_env
    await session.flush()
    # Pre-dispatch commit (ADR-0046): identifiers, frozen XML, counters and
    # the lease become durable; the row locks release. From here on the DB
    # always knows which file may exist at SdI, whatever happens next.
    await tenant_checkpoint(session)

    # PHASE 2 -- dispatch. No lock, no open invoice transaction: the SdI
    # round-trip blocks nobody, and a crash here leaves a retryable invoice,
    # not an unrecorded file. The wall time is bounded explicitly (httpx's
    # timeout is per phase, not total) so an expired lease provably implies
    # no in-flight dispatch. asyncio cancellation (client disconnect) is
    # deliberately NOT caught: the lease expiry handles that path.
    try:
        async with asyncio.timeout(get_settings().sdi_dispatch_timeout_seconds):
            res = await ch.transmit(xml=xml, invoice_id=str(inv.id), filename=filename)
    except Exception as exc:
        await _record_dispatch_failure(
            session,
            org_id=org_id,
            actor_id=actor_id,
            invoice_id=inv.id,
            exc=exc,
            retrying=retrying,
        )
        if isinstance(exc, DomainError):
            raise
        if _dispatch_definitely_not_filed(exc) and not retrying:
            # Provably nothing at SdI: surface the transport error as-is
            # (the invoice is back in draft, identifiers kept for reuse).
            raise
        raise ConflictError(MessageCode.INVOICE_TRANSMIT_UNCONFIRMED) from exc

    # PHASE 3 -- success. Re-lock (fresh read) and record; rides the caller's
    # transaction so the public API's idempotency snapshot commits atomically
    # with it. If the inbound reconcile adopted an identifier while the
    # dispatch was in flight (a late RC for the previous attempt's filing),
    # the reconciled state wins: this attempt's sync ident belongs to a
    # duplicate filing that SdI will scarto as a NomeFile echo.
    # include_deleted: recording the outcome of a file that already left must
    # not depend on the trash flag (a concurrent trash landed in the unlocked
    # dispatch window).
    inv = await get_invoice(
        session, org_id=org_id, invoice_id=invoice_id, for_update=True, include_deleted=True
    )
    if inv.state is InvoiceState.transmitted and inv.identificativo_sdi is None:
        inv.identificativo_sdi = res.identificativo_sdi
        inv.conservation_status = res.conservation
    inv.sdi_dispatch_started_at = None
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
    # ADR-0047: signed webhook. SAVEPOINT-wrapped in the service -- a fan-out
    # fault can never abort this fiscal write. Dedupe on the invoice so the
    # 'transmitted' event fires exactly once even across resend legs.
    await webhooks_svc.enqueue_invoice_event(
        session,
        org_id=org_id,
        invoice=inv,
        event_type=webhooks_svc.EVENT_TRANSMITTED,
        dedupe_key=f"transmitted:{inv.id}",
        occurred_at=dt.datetime.now(tz=dt.UTC),
    )
    return inv


async def reopen_rejected(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, invoice_id: uuid.UUID
) -> Invoice:
    """Return a scartato (SdI NS / ``rejected``) invoice to ``draft`` so it can
    be corrected and re-transmitted. FatturaPA: a scartato document was never
    validly issued, so within 5 days it is re-sent under the SAME numero + data
    (kept here; ``transmit`` reuses them). The stale rejected XML and the SdI
    correlation are cleared so the next transmit rebuilds and re-files. Only a
    ``rejected`` invoice may be reopened -- a delivered/accepted one is
    corrected with a TD04 credit note, never reopened."""
    await require_role(session, org_id, actor_id, Role.member)
    inv = await get_invoice(session, org_id=org_id, invoice_id=invoice_id, for_update=True)
    if inv.state is not InvoiceState.rejected:
        raise ConflictError(MessageCode.INVOICE_NOT_REJECTED)
    inv.state = InvoiceState.draft
    inv.xml = None
    inv.identificativo_sdi = None
    inv.sdi_status = SdiStatus.none
    # Not under AdE conservation until it actually transits SdI on the re-send.
    inv.conservation_status = ConservationStatus.out_of_coverage
    # A scartato file name is spent: the re-send draws a fresh ProgressivoInvio /
    # NomeFile so the legitimate resend is not itself rejected as a duplicate.
    inv.progressivo_invio = None
    inv.nome_file = None
    inv.sdi_dispatch_started_at = None
    inv.sdi_resent_at = None
    # Drop the superseded transmission's SdI notifications: the scarto belonged
    # to the file being redone, so keeping it would make the reopened+resent
    # invoice still show the old NS ("rejected") in its timeline. A fresh outcome
    # after the re-send adds a new row.
    await session.execute(
        delete(InvoiceNotification).where(InvoiceNotification.invoice_id == inv.id)
    )
    inv.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="invoice",
        entity_id=inv.id,
        action="reopen",
        diff={"from": "rejected", "number": f"{inv.series}-{inv.number}"},
    )
    return inv


async def create_credit_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    parent_invoice_id: uuid.UUID,
    purpose: str | None = None,
) -> Invoice:
    """TD04 credit note linked to a transmitted invoice (ADR-0009: the
    only post-emission correction). Copies the parent's lines."""
    purpose = validate_purpose(purpose)
    parent = await get_invoice(session, org_id=org_id, invoice_id=parent_invoice_id)
    # A TD04 corrects an EMITTED invoice. A draft is not yet issued; a scartato
    # (rejected) one was never validly issued -> it is corrected by resend
    # (reopen_rejected), not by a credit note against a non-existent document.
    if parent.state not in (
        InvoiceState.transmitted,
        InvoiceState.delivered,
        InvoiceState.accepted,
    ):
        raise ConflictError(MessageCode.CREDIT_NOTE_PARENT_INVALID)
    if (
        parent.state is InvoiceState.transmitted
        and parent.identificativo_sdi is None
        and parent.sdi_dispatch_started_at is not None
    ):
        # The parent's dispatch has not settled (ADR-0046 in-flight or
        # unconfirmed window): correcting a document that may not exist at
        # SdI yet is premature -- retry once the transmission settles.
        raise ConflictError(MessageCode.INVOICE_TRANSMIT_IN_PROGRESS)
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
        purpose=purpose,
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
            vat_nature=ln.vat_nature,
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
    await webhooks_svc.enqueue_invoice_event(
        session,
        org_id=org_id,
        invoice=inv,
        event_type=webhooks_svc.EVENT_PAYMENT_RECORDED,
        dedupe_key=f"payment_recorded:{inv.id}",
        occurred_at=dt.datetime.now(tz=dt.UTC),
    )
    return inv


async def soft_delete_invoice(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, invoice_id: uuid.UUID
) -> Invoice:
    """Move an invoice to the recycle bin (reversible). Allowed in any
    SETTLED state: trashing only hides the row, it never deletes the
    document, so it does not break the immutability of a transmitted invoice
    (which is kept for the fiscal record and can only be restored, never
    purged). An UNSETTLED dispatch (ADR-0046: transmitted, ident-less, lease
    set) is refused: its outcome still has to be recorded/reconciled, and
    hiding the row mid-flight would strand the retry/resume path."""
    inv = await get_invoice(session, org_id=org_id, invoice_id=invoice_id)
    await require_role(session, org_id, actor_id, Role.member)
    if (
        inv.state is InvoiceState.transmitted
        and inv.identificativo_sdi is None
        and inv.sdi_dispatch_started_at is not None
    ):
        raise ConflictError(MessageCode.INVOICE_TRANSMIT_IN_PROGRESS)
    inv.deleted_at = dt.datetime.now(tz=dt.UTC)
    inv.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="invoice",
        entity_id=invoice_id,
        action="trash",
    )
    return inv


async def restore_invoice(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, invoice_id: uuid.UUID
) -> Invoice:
    """Restore a trashed invoice back to the active list."""
    inv = await get_invoice(session, org_id=org_id, invoice_id=invoice_id, include_deleted=True)
    await require_role(session, org_id, actor_id, Role.member)
    inv.deleted_at = None
    inv.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="invoice",
        entity_id=invoice_id,
        action="restore",
    )
    return inv


async def archive_invoice(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    invoice_id: uuid.UUID,
    archived: bool = True,
) -> Invoice:
    """Archive (year-end filing) or unarchive an invoice. Reversible and
    document-preserving: only the visibility changes, not the content."""
    inv = await get_invoice(session, org_id=org_id, invoice_id=invoice_id)
    await require_role(session, org_id, actor_id, Role.member)
    inv.is_archived = archived
    inv.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="invoice",
        entity_id=invoice_id,
        action="archive" if archived else "unarchive",
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

# The SdI scarto code meaning "this FILE NAME was already filed": 00002 nome
# file duplicato. A byte-identical resend under the same NomeFile (the only
# resend the two-phase transmit produces) is rejected at SdI's nomenclature
# stage with 00002 alone, so an NS carrying ONLY 00002 is the dedupe echo of
# a re-sent file whose original filing lives on (ADR-0046) -- it must not
# reject the invoice. NOTE: 00404 "fattura duplicata" is deliberately NOT
# here: content checks never run on a name-deduped resend, so a 00404 always
# refers to a genuine competing filing (e.g. the same numero sent through
# another channel) and must reject normally.
_DUPLICATE_ECHO_CODES = frozenset({"00002"})


def _is_duplicate_echo(raw_xml: bytes) -> bool:
    """True when an NS's error list is non-empty and every code is a
    duplicate-echo code (00002). A mixed scarto (a real content error
    alongside the duplicate code) is NOT an echo and rejects normally."""
    from mycelium_core.services.sdi_inbound import parse_scarto_errors

    errors = parse_scarto_errors(raw_xml)
    codes = {e["codice"] for e in errors if e.get("codice")}
    return bool(codes) and codes <= _DUPLICATE_ECHO_CODES


async def _archive_notification_without_transition(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    inv: Invoice,
    parsed: ParsedNotificationLike,
    reason: str,
) -> Invoice:
    """Store a notification for the audit trail WITHOUT any state transition:
    either a duplicate-echo NS (the scarto only proves the same document
    already lives at SdI) or a filename-fallback match against an invoice
    that is not in the in-flight shape (state guard)."""
    log.warning(
        "sdi notification archived without transition (%s): invoice=%s outcome=%s ident=%s",
        reason,
        inv.id,
        parsed.outcome,
        parsed.identificativo_sdi,
    )
    notif = InvoiceNotification(
        org_id=org_id,
        invoice_id=inv.id,
        kind=parsed.outcome,
        file_name=parsed.file_name,
        message_id=parsed.message_id,
        raw_xml=parsed.raw_xml,
        payload={"outcome": parsed.outcome, "stored_without_transition": reason},
    )
    session.add(notif)
    try:
        await session.flush()
    except IntegrityError:
        # Same-notification redelivery; the first insertion won. Re-arm the
        # GUCs so the rest of the request keeps its tenant context.
        await tenant_rollback(session)
        return inv
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="invoice",
        entity_id=inv.id,
        action="sdi_notification",
        diff={"outcome": parsed.outcome, "stored_without_transition": reason},
    )
    return inv


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
    idempotent.

    Lost-ACK reconcile (ADR-0046): when the IdentificativoSdI matches no
    invoice (the sync ACK of the dispatch was lost, so it was never stored),
    the notification is correlated by ``NomeFile`` instead -- the file name is
    committed BEFORE dispatch, so it is always on record. An ident-less
    invoice ADOPTS the notification's identifier. When the invoice already
    carries a DIFFERENT identifier (a retry re-sent the same file and both
    filings raced), the real filing's outcome wins: positive outcomes adopt
    the incoming identifier; an NS whose errors are all duplicate-echo codes
    (00002 nome file duplicato / 00404 fattura duplicata) is evidence the
    SAME document already lives at SdI, so it is archived without any state
    transition."""
    # FOR UPDATE + populate_existing: the ingest races the two-phase
    # transmit's unlocked dispatch window, so the row must be locked and
    # read FRESH (a stale identity-map object would clobber a concurrent
    # phase-3 write, and vice versa).
    inv = (
        await session.execute(
            select(Invoice)
            .where(Invoice.identificativo_sdi == parsed.identificativo_sdi)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if inv is None and parsed.file_name:
        inv = (
            await session.execute(
                select(Invoice)
                .where(Invoice.nome_file == parsed.file_name)
                .order_by(Invoice.created_at.desc())
                .limit(1)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    if inv is None:
        raise NotFoundError(MessageCode.INVOICE_NOT_FOUND)
    adopted = False
    adopted_from: str | None = None
    if inv.identificativo_sdi != parsed.identificativo_sdi:
        if parsed.outcome == "NS" and _is_duplicate_echo(parsed.raw_xml):
            return await _archive_notification_without_transition(
                session,
                org_id=org_id,
                actor_id=actor_id,
                inv=inv,
                parsed=parsed,
                reason="duplicate_echo",
            )
        if inv.state is not InvoiceState.transmitted:
            # Filename-fallback match against an invoice that is not in the
            # in-flight shape (e.g. a definite-fail revert kept the name on a
            # draft, or the document already settled under another filing):
            # never adopt, never transition -- archive for the trail.
            return await _archive_notification_without_transition(
                session,
                org_id=org_id,
                actor_id=actor_id,
                inv=inv,
                parsed=parsed,
                reason="state_guard",
            )
        # Adopt the incoming identifier: either the invoice never had one
        # (lost ACK) or the incoming filing is the one SdI actually processed.
        adopted = True
        adopted_from = inv.identificativo_sdi
        inv.identificativo_sdi = parsed.identificativo_sdi
    elif (
        parsed.outcome == "NS"
        and inv.sdi_resent_at is not None
        and _is_duplicate_echo(parsed.raw_xml)
    ):
        # Same identifier, but the scarto only says "this file name was
        # already filed", and we HAVE a persisted proof that this invoice was
        # re-sent (``sdi_resent_at``, stamped on the retry leg): the resend
        # echoed off SdI's dedupe while the original filing lives on. Not a
        # rejection of the document. Without that proof a pure-00002 NS on
        # the recorded identifier is a genuine scarto (e.g. a first send
        # whose name was burned by a pre-ADR-0046 rollback) and must reject
        # normally, or the 5-day correction window would be silently missed.
        return await _archive_notification_without_transition(
            session,
            org_id=org_id,
            actor_id=actor_id,
            inv=inv,
            parsed=parsed,
            reason="duplicate_echo",
        )
    # Snapshot the pre-transition state so the webhook only fires on an ACTUAL
    # transition (a redelivery of a settling notification returns early below;
    # a second RC/AT that does not move the state must not re-fire).
    prev_state = inv.state
    webhook_event: str | None = None
    now = dt.datetime.now(tz=dt.UTC)
    payload: dict[str, object] = {}
    if parsed.outcome in _RECEIPT_MAP:
        sdi, state, cons = _RECEIPT_MAP[parsed.outcome]
        inv.sdi_status = sdi
        inv.state = state
        inv.conservation_status = cons
        payload = {"outcome": parsed.outcome}
        webhook_event = (
            webhooks_svc.EVENT_DELIVERED
            if state is InvoiceState.delivered
            else webhooks_svc.EVENT_REJECTED
        )
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
        webhook_event = (
            webhooks_svc.EVENT_ACCEPTED
            if state is InvoiceState.accepted
            else webhooks_svc.EVENT_REJECTED
        )
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
            webhook_event = webhooks_svc.EVENT_DEEMED_ACCEPTED
        payload = {"outcome": "DT"}
    else:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    if adopted:
        # Trace the lost-ACK / raced-resend reconcile in the audit payload.
        payload["adopted_identificativo_from"] = adopted_from or ""
    # A notification settling the invoice ends any unsettled dispatch: drop
    # the lease so the invoice stops advertising itself as retry-pending
    # (SPA affordance, public sdi_dispatch_started_at, credit-note guard).
    inv.sdi_dispatch_started_at = None
    inv.version += 1

    notif = InvoiceNotification(
        org_id=org_id,
        invoice_id=inv.id,
        kind=parsed.outcome,
        file_name=parsed.file_name,
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
        # audit row that wins (the first insertion); re-arm the GUCs so the
        # rest of the request keeps its tenant context.
        await tenant_rollback(session)
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
    # ADR-0047: fire only on a real state transition (SAVEPOINT-wrapped; a
    # webhook fault can never abort this fiscal ingest write).
    if webhook_event is not None and inv.state != prev_state:
        await webhooks_svc.enqueue_invoice_event(
            session,
            org_id=org_id,
            invoice=inv,
            event_type=webhook_event,
            dedupe_key=f"{webhook_event}:{inv.id}:{parsed.message_id}",
            occurred_at=now,
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
from typing import Literal, Protocol  # noqa: E402

InvoiceView = Literal["active", "archived", "trashed"]


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
    def file_name(self) -> str | None: ...
    @property
    def esito(self) -> str | None: ...
    @property
    def raw_xml(self) -> bytes: ...


async def list_invoices(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    state: InvoiceState | None = None,
    states: Sequence[InvoiceState] | None = None,
    payment_status: PaymentStatus | None = None,
    client_tag_id: uuid.UUID | None = None,
    issuer_profile_id: uuid.UUID | None = None,
    view: InvoiceView = "active",
) -> list[Invoice]:
    """List invoices, newest first. ``state`` (single) is kept for
    existing callers; ``states`` (the lifecycle multi-select used by the
    SPA) takes precedence when given. ``payment_status`` is an orthogonal
    axis (paid/unpaid), AND-ed with the state filter. ``view`` selects the
    visibility band (orthogonal to everything else): ``active`` (default)
    hides trashed and archived; ``archived`` shows filed-away non-trashed;
    ``trashed`` shows the recycle bin."""
    stmt = select(Invoice)
    if view == "trashed":
        stmt = stmt.where(Invoice.deleted_at.is_not(None))
    elif view == "archived":
        stmt = stmt.where(Invoice.deleted_at.is_(None), Invoice.is_archived.is_(True))
    else:
        stmt = stmt.where(Invoice.deleted_at.is_(None), Invoice.is_archived.is_(False))
    if states:
        stmt = stmt.where(Invoice.state.in_(list(states)))
    elif state is not None:
        stmt = stmt.where(Invoice.state == state)
    if payment_status is not None:
        stmt = stmt.where(Invoice.payment_status == payment_status)
    if client_tag_id is not None:
        stmt = stmt.where(Invoice.client_tag_id == client_tag_id)
    if issuer_profile_id is not None:
        # Hard issuer scoping for the per-issuer-key API surface (task 19b7e874).
        stmt = stmt.where(Invoice.issuer_profile_id == issuer_profile_id)
    stmt = stmt.order_by(Invoice.year.desc(), Invoice.number.desc().nullslast())
    return list((await session.execute(stmt)).scalars().all())


async def list_invoice_changes(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    issuer_profile_id: uuid.UUID,
    since: dt.datetime | None = None,
    limit: int = 100,
) -> list[Invoice]:
    """Issuer-scoped 'events' feed: invoices whose state last changed after
    ``since`` (their ``updated_at``), oldest change first, so the public API can
    hand integrators a cursor over changes instead of the whole table. The
    caller advances ``since`` to the last row's ``updated_at`` (task 19b7e874)."""
    stmt = select(Invoice).where(Invoice.issuer_profile_id == issuer_profile_id)
    if since is not None:
        stmt = stmt.where(Invoice.updated_at > since)
    stmt = stmt.order_by(Invoice.updated_at.asc(), Invoice.id.asc()).limit(min(max(limit, 1), 500))
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
    # Each line's AltriDatiGestionali blocks (FatturaPA 2.2.1.16) keyed
    # by line id, in emission order; a line with none is simply absent.
    # Resolved here with the lines so the XML/PDF/JSON renderings all
    # read one already-loaded set instead of querying per line.
    altri_dati: dict[uuid.UUID, list[InvoiceLineAltriDati]]
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
        altri_dati=await list_invoice_altri_dati(session, org_id=org_id, invoice_id=inv.id),
        totals=totals,
        effective_iban=iban,
        iban_source=src,
        is_forfettario=_is_forfettario(issuer),
        # ``<sezionale>-<counter>`` (e.g. ``EXAMPLE-2``). The hyphen
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
    inv.taxable, inv.vat, inv.stamp_duty, inv.total = (
        p.totals.taxable,
        p.totals.vat,
        p.totals.stamp_duty,
        p.totals.total,
    )
    collegata = await _resolve_collegata(session, org_id=org_id, inv=inv)
    xml = _build_xml(
        inv,
        p.issuer,
        p.client,
        p.lines,
        "ANTEPRIMA",
        numero_override=p.number,
        collegata=collegata,
        # Same trasmittente resolution as transmit(), so the ANTEPRIMA carries
        # the IdTrasmittente the real send will carry -- for a self-transmitting
        # issuer that is the cedente's own codice fiscale, not the channel's
        # P.IVA, and the two differ for a physical-person channel holder.
        intermediary=_payload_intermediary(p.issuer, get_channel().intermediary),
        # Already loaded by _gather_preview: the ANTEPRIMA must carry the
        # same AltriDatiGestionali transmit() will freeze (2.2.1.16).
        altri_dati=p.altri_dati,
    )
    # The same official-schema gate transmit() applies, for the same reason and
    # with the same error. Without it the preview was byte-faithful but not
    # VALIDITY-faithful: it handed back a downloadable document that transmit
    # would refuse, and a connector dry-run reported a clean shadow run on a
    # document that could never be filed. Two live defects (a euro sign in a
    # line description, a six-digit CAP on an issuer profile) reached a
    # downloaded artifact that way, with the authoritative schema sitting in
    # the tree and validation costing one call.
    xsd_errors = validate_fatturapa(xml)
    if xsd_errors:
        raise DomainError(MessageCode.INVOICE_INVALID, detail="; ".join(xsd_errors[:5]))
    return xml


async def render_pdf(
    session: AsyncSession, *, org_id: uuid.UUID, invoice_id: uuid.UUID
) -> tuple[str, bytes]:
    """Courtesy A4 PDF of the document (draft preview or emitted).
    Validates like the XML preview; returns (number, pdf_bytes)."""
    from mycelium_core.services.invoice_pdf import build_pdf

    inv = await get_invoice(session, org_id=org_id, invoice_id=invoice_id)
    p = await _gather_preview(session, org_id=org_id, inv=inv)
    _validate(p.issuer, p.client or ClientProfile(), p.lines)
    assert p.issuer is not None and p.client is not None  # _validate raised  # noqa: S101
    # Pass the resolved would-be number (e.g. "EXAMPLE2") so the PDF
    # shows what the document will become, with the state as a smaller
    # tag — a transmitted invoice prints the real number; a draft
    # prints the prospective number + a DRAFT/BOZZA marker. Surfacing
    # "BOZZA" alone hides the information the user actually needs
    # (which number am I about to emit?).
    is_draft = inv.state == InvoiceState.draft
    # Load the (deferred) letterhead logo only here, on the PDF path.
    logo_row = await get_issuer_logo(session, org_id=org_id, profile_id=p.issuer.id)
    pdf = build_pdf(
        inv,
        p.issuer,
        p.client,
        p.lines,
        p.totals,
        number=p.number,
        is_draft=is_draft,
        logo=logo_row[0] if logo_row else None,
        # The owner wants a filled block visible on the courtesy copy as
        # well as in the XML; a line with none renders exactly as before.
        altri_dati=p.altri_dati,
    )
    return p.number, pdf
