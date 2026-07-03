"""Public Invoice API (``/api/v1``) authenticated by a per-issuer-profile API key.

Task 19b7e874, phase 3. Every route depends on ``issuer_key_ctx`` (H1: the
principal is the key, role pinned to ``member``), and every by-id route goes
through ``_load_invoice_for_key`` (H2: hard issuer scoping, 404 on a mismatch so
the surface is not a cross-issuer oracle). State-changing routes require an
``Idempotency-Key`` header and claim it atomically (no double-filing).

This module owns only the ACCESS layer; it wraps the existing
``mycelium_core.services.invoice`` service unchanged.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from mycelium_api import idempotency as idem
from mycelium_api import rate_limit
from mycelium_api.deps import IssuerKeyCtx, issuer_key_ctx, require_perm
from mycelium_core.config import get_settings
from mycelium_core.errors import DomainError, NotFoundError, UnprocessableError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.invoice import (
    BuyerVerdict,
    ConservationStatus,
    DocumentType,
    Invoice,
    InvoiceState,
    PaymentStatus,
    SdiStatus,
)
from mycelium_core.models.sdi_notification import InvoiceNotification
from mycelium_core.services import invoice as invoice_svc
from mycelium_core.services import taxonomy
from mycelium_core.services.issuer_api_keys import (
    PERM_CLIENT_WRITE,
    PERM_COMPOSE,
    PERM_CREDIT_NOTE,
    PERM_DOWNLOAD,
    PERM_READ,
    PERM_SEND,
)
from mycelium_core.services.taxonomy import ClientInput

router = APIRouter(prefix="/api/v1", tags=["public-invoices"])

_NO_STORE = {"Cache-Control": "no-store"}


# --- request / response models --------------------------------------------


class PublicClientIn(BaseModel):
    """Inline cessionario. Requires the ``invoice:client_write`` permission;
    resolved-or-created idempotently by (country, VAT) / codice fiscale."""

    legal_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    country_code: str | None = None
    vat_number: str | None = None
    tax_code: str | None = None
    address: str | None = None
    civic_number: str | None = None
    postal_code: str | None = None
    city: str | None = None
    province: str | None = None
    country: str | None = None
    sdi_code: str | None = None
    pec: str | None = None


class PublicLineIn(BaseModel):
    description: str
    unit_price: Decimal
    quantity: Decimal = Decimal(1)
    vat_rate: Decimal | None = None
    vat_nature: str | None = None


class PublicComposeIn(BaseModel):
    # Exactly one recipient form: an existing client tag, or inline data.
    client_tag_id: uuid.UUID | None = None
    client: PublicClientIn | None = None
    series: str | None = None
    purpose: str | None = None
    lines: list[PublicLineIn] = Field(min_length=1)
    transmit: bool = False


class PublicBatchItemIn(BaseModel):
    """One draft in a batch compose. Same recipient/lines shape as the
    per-invoice compose, but transmit is deliberately absent: a batch is
    compose-only (drafts, no number allocation, no SdI), so the fiscal
    numbering + ADR-0046 dispatch durability stay on the per-invoice transmit
    path. Transmit each returned draft id individually."""

    client_tag_id: uuid.UUID | None = None
    client: PublicClientIn | None = None
    series: str | None = None
    purpose: str | None = None
    lines: list[PublicLineIn] = Field(min_length=1)


class PublicBatchIn(BaseModel):
    items: list[PublicBatchItemIn] = Field(min_length=1)


class PublicCreditNoteIn(BaseModel):
    parent_invoice_id: uuid.UUID
    purpose: str | None = None


class PublicInvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    client_tag_id: uuid.UUID
    issuer_profile_id: uuid.UUID | None
    document_type: DocumentType
    series: str
    year: int
    number: int | None
    state: InvoiceState
    sdi_status: SdiStatus
    payment_status: PaymentStatus
    buyer_verdict: BuyerVerdict
    conservation_status: ConservationStatus
    currency: str
    taxable: Decimal
    vat: Decimal
    stamp_duty: Decimal
    total: Decimal
    identificativo_sdi: str | None
    # Non-null while a dispatch is unsettled (ADR-0046): a transmit that
    # returned 409 invoice.transmit_unconfirmed stays in this shape until a
    # retry (same Idempotency-Key resumes it) or the inbound reconcile
    # settles it. Integrators can poll this field to distinguish an
    # unsettled dispatch from a settled ident-less manual export.
    sdi_dispatch_started_at: datetime.datetime | None
    purpose: str | None


class PublicBatchItemOut(BaseModel):
    index: int
    status: str  # "created" | "error"
    invoice: PublicInvoiceOut | None = None
    error_code: str | None = None
    error_detail: str | None = None


class PublicBatchOut(BaseModel):
    created: int
    failed: int
    results: list[PublicBatchItemOut]


class PublicNotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    message_id: str | None


class PublicEventOut(BaseModel):
    """One entry in the state-change feed (``GET /events?since=``)."""

    invoice_id: uuid.UUID
    state: InvoiceState
    sdi_status: SdiStatus
    updated_at: datetime.datetime


def _out(inv: Invoice) -> PublicInvoiceOut:
    return PublicInvoiceOut.model_validate(inv)


# --- helpers ---------------------------------------------------------------


def _require_idem(idempotency_key: str | None) -> str:
    if not idempotency_key:
        raise DomainError(MessageCode.IDEMPOTENCY_KEY_REQUIRED)
    return idempotency_key


_CLASS_LIMIT: dict[str, str] = {
    "read": "issuer_key_rate_limit_read",
    "write": "issuer_key_rate_limit_write",
    "transmit": "issuer_key_rate_limit_transmit",
}


async def _rate(ctx: IssuerKeyCtx, endpoint_class: str) -> None:
    """Per-key shared-store rate limit; 429 past the class budget."""
    s = get_settings()
    await rate_limit.check(
        ctx.session,
        org_id=ctx.org_id,
        key_id=ctx.key_id,
        endpoint_class=endpoint_class,
        limit=getattr(s, _CLASS_LIMIT[endpoint_class]),
        window_seconds=s.issuer_key_rate_window_seconds,
    )


async def _load_invoice_for_key(ctx: IssuerKeyCtx, invoice_id: uuid.UUID) -> Invoice:
    """H2: the single guarded by-id loader. RLS already scopes to the org; this
    adds the issuer check and returns 404 (never 403) on a mismatch so a key
    cannot probe another issuer's invoice ids."""
    inv = await invoice_svc.get_invoice(ctx.session, org_id=ctx.org_id, invoice_id=invoice_id)
    if inv.issuer_profile_id != ctx.issuer_profile_id:
        raise NotFoundError(MessageCode.INVOICE_NOT_FOUND)
    return inv


async def _resume_transmit(ctx: IssuerKeyCtx, invoice_id: uuid.UUID) -> Invoice:
    """Resume leg of a same-key retry on an unsettled dispatch (ADR-0046).

    The claim was committed by the pre-dispatch checkpoint with its invoice
    attached; if the invoice SETTLED in the meantime (the inbound reconcile
    adopted the identifier, or a crashed attempt's success was recovered),
    the idempotent outcome of the original request is the invoice's CURRENT
    state -- calling transmit again would 409 on a successfully filed
    document and poison the key forever. Only a still-retryable shape (a
    reverted draft, or parked transmitted with the lease set) re-dispatches;
    the in-flight case is refused by the lease inside ``transmit``."""
    inv = await _load_invoice_for_key(ctx, invoice_id)
    retryable = inv.state is InvoiceState.draft or (
        inv.state is InvoiceState.transmitted
        and inv.identificativo_sdi is None
        and inv.sdi_dispatch_started_at is not None
    )
    if retryable:
        return await invoice_svc.transmit(
            ctx.session, org_id=ctx.org_id, actor_id=ctx.key_id, invoice_id=invoice_id
        )
    return inv


async def _resolve_recipient(ctx: IssuerKeyCtx, body: PublicComposeIn) -> uuid.UUID:
    if (body.client_tag_id is None) == (body.client is None):
        raise UnprocessableError(MessageCode.COMPOSE_RECIPIENT_INVALID)
    if body.client_tag_id is not None:
        return body.client_tag_id
    # Inline recipient -> the distinct client-write capability (confused-deputy
    # fix): a compose-only key must reference an existing client_tag_id.
    require_perm(ctx, PERM_CLIENT_WRITE)
    ci = body.client
    assert ci is not None  # narrowed by the XOR check above  # noqa: S101
    name = ci.legal_name or " ".join(p for p in (ci.first_name, ci.last_name) if p) or "Cliente"
    tag = await taxonomy.resolve_or_create_client(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.key_id,
        name=name,
        profile=ClientInput(
            legal_name=ci.legal_name or name,
            first_name=ci.first_name,
            last_name=ci.last_name,
            country_code=ci.country_code,
            vat_number=ci.vat_number,
            tax_code=ci.tax_code,
            address=ci.address,
            civic_number=ci.civic_number,
            postal_code=ci.postal_code,
            city=ci.city,
            province=ci.province,
            country=ci.country,
            sdi_code=ci.sdi_code,
            pec=ci.pec,
        ),
    )
    return tag.id


async def _compose_draft(ctx: IssuerKeyCtx, body: PublicComposeIn) -> Invoice:
    """Resolve the recipient, create a TD01 draft, append the lines, and return
    it re-read (so add_line's recomputed totals are reflected). The single
    draft-creation path -- both the per-invoice compose and the batch endpoint
    go through here, so they can never drift."""
    client_tag_id = await _resolve_recipient(ctx, body)
    inv = await invoice_svc.create_draft(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.key_id,
        client_tag_id=client_tag_id,
        issuer_profile_id=ctx.issuer_profile_id,
        series=body.series,
        purpose=body.purpose,
        document_type=DocumentType.TD01,
    )
    for ln in body.lines:
        await invoice_svc.add_line(
            ctx.session,
            org_id=ctx.org_id,
            actor_id=ctx.key_id,
            invoice_id=inv.id,
            description=ln.description,
            unit_price=ln.unit_price,
            quantity=ln.quantity,
            vat_rate=ln.vat_rate,
            vat_nature=ln.vat_nature,
        )
    return await invoice_svc.get_invoice(ctx.session, org_id=ctx.org_id, invoice_id=inv.id)


# --- endpoints -------------------------------------------------------------


@router.post("/invoices", response_model=PublicInvoiceOut)
async def compose(
    body: PublicComposeIn,
    ctx: Annotated[IssuerKeyCtx, Depends(issuer_key_ctx)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> PublicInvoiceOut:
    """Compose a TD01 draft (header + lines); optionally transmit in one shot."""
    require_perm(ctx, PERM_COMPOSE)
    if body.transmit:
        require_perm(ctx, PERM_SEND)
    await _rate(ctx, "transmit" if body.transmit else "write")
    key = _require_idem(idempotency_key)
    req_hash = idem.request_digest(body.model_dump(mode="json"))
    claimed = await idem.claim(
        ctx.session,
        org_id=ctx.org_id,
        issuer_profile_id=ctx.issuer_profile_id,
        endpoint="compose",
        idempotency_key=key,
        request_hash=req_hash,
    )
    if claimed.replay is not None:
        return PublicInvoiceOut.model_validate(claimed.replay)
    assert claimed.row_id is not None  # claim row exists on every non-replay leg  # noqa: S101
    if claimed.resume_invoice_id is not None:
        # A prior attempt with this key committed its pre-dispatch state but
        # never settled (ADR-0046): resume THAT invoice instead of composing
        # a duplicate (the lease arbitrates liveness; a settled invoice is
        # returned as-is).
        inv = await _resume_transmit(ctx, claimed.resume_invoice_id)
    else:
        inv = await _compose_draft(ctx, body)
        if body.transmit:
            # Bind the claim to the draft BEFORE the dispatch so the
            # pre-dispatch commit persists the pair and a retry resumes.
            await idem.attach_invoice(ctx.session, row_id=claimed.row_id, invoice_id=inv.id)
            inv = await invoice_svc.transmit(
                ctx.session, org_id=ctx.org_id, actor_id=ctx.key_id, invoice_id=inv.id
            )
    out = _out(inv)
    await idem.store(
        ctx.session, row_id=claimed.row_id, snapshot=out.model_dump(mode="json"), invoice_id=inv.id
    )
    return out


@router.post("/invoices/batch", response_model=PublicBatchOut)
async def compose_batch(
    body: PublicBatchIn,
    ctx: Annotated[IssuerKeyCtx, Depends(issuer_key_ctx)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> PublicBatchOut:
    """Compose up to ``issuer_batch_max_items`` TD01 drafts in one request.

    COMPOSE-ONLY: no transmit (so no number allocation, no SdI) -- transmit each
    returned draft id on the per-invoice path, which owns the ADR-0046 dispatch
    durability + fiscal-numbering guarantees. BEST-EFFORT: each item runs in its
    own SAVEPOINT, so one bad item (invalid recipient/line) becomes a per-item
    error without rolling back the rest. Idempotency is at the BATCH level:
    replaying the same Idempotency-Key + body returns the stored results without
    re-creating drafts. Charged as one ``write`` against the per-key rate limit
    (the item cap bounds the work; per-item charging would defeat bulk)."""
    require_perm(ctx, PERM_COMPOSE)
    cap = get_settings().issuer_batch_max_items
    if len(body.items) > cap:
        raise UnprocessableError(MessageCode.INVOICE_BATCH_TOO_LARGE, detail=f"max {cap} per batch")
    await _rate(ctx, "write")
    key = _require_idem(idempotency_key)
    req_hash = idem.request_digest(body.model_dump(mode="json"))
    claimed = await idem.claim(
        ctx.session,
        org_id=ctx.org_id,
        issuer_profile_id=ctx.issuer_profile_id,
        endpoint="compose_batch",
        idempotency_key=key,
        request_hash=req_hash,
    )
    if claimed.replay is not None:
        return PublicBatchOut.model_validate(claimed.replay)
    assert claimed.row_id is not None  # claim row exists on every non-replay leg  # noqa: S101
    results: list[PublicBatchItemOut] = []
    for i, item in enumerate(body.items):
        as_compose = PublicComposeIn(
            client_tag_id=item.client_tag_id,
            client=item.client,
            series=item.series,
            purpose=item.purpose,
            lines=item.lines,
            transmit=False,
        )
        try:
            async with ctx.session.begin_nested():
                inv = await _compose_draft(ctx, as_compose)
            results.append(PublicBatchItemOut(index=i, status="created", invoice=_out(inv)))
        except DomainError as exc:
            # Per-item isolation: the SAVEPOINT rolled this item's partial work
            # back; record the error and keep going.
            results.append(
                PublicBatchItemOut(
                    index=i, status="error", error_code=exc.code.value, error_detail=str(exc)
                )
            )
    created = sum(1 for r in results if r.status == "created")
    out = PublicBatchOut(created=created, failed=len(results) - created, results=results)
    await idem.store(ctx.session, row_id=claimed.row_id, snapshot=out.model_dump(mode="json"))
    return out


@router.post("/invoices/{invoice_id}/transmit", response_model=PublicInvoiceOut)
async def transmit(
    invoice_id: uuid.UUID,
    ctx: Annotated[IssuerKeyCtx, Depends(issuer_key_ctx)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> PublicInvoiceOut:
    require_perm(ctx, PERM_SEND)
    await _rate(ctx, "transmit")
    key = _require_idem(idempotency_key)
    await _load_invoice_for_key(ctx, invoice_id)  # H2 scope check (404 on mismatch)
    req_hash = idem.request_digest({"invoice_id": str(invoice_id)})
    claimed = await idem.claim(
        ctx.session,
        org_id=ctx.org_id,
        issuer_profile_id=ctx.issuer_profile_id,
        endpoint="transmit",
        idempotency_key=key,
        request_hash=req_hash,
    )
    if claimed.replay is not None:
        return PublicInvoiceOut.model_validate(claimed.replay)
    assert claimed.row_id is not None  # claim row exists on every non-replay leg  # noqa: S101
    if claimed.is_new:
        # Bind the claim to its invoice BEFORE the dispatch: the pre-dispatch
        # commit (ADR-0046) then persists the pair, and a retry after an
        # unsettled dispatch resumes instead of 409ing forever.
        await idem.attach_invoice(ctx.session, row_id=claimed.row_id, invoice_id=invoice_id)
        inv = await invoice_svc.transmit(
            ctx.session, org_id=ctx.org_id, actor_id=ctx.key_id, invoice_id=invoice_id
        )
    else:
        # Resume leg: targets the same invoice by construction (the request
        # hash pins invoice_id); a settled invoice is returned as-is.
        inv = await _resume_transmit(ctx, invoice_id)
    out = _out(inv)
    await idem.store(
        ctx.session, row_id=claimed.row_id, snapshot=out.model_dump(mode="json"), invoice_id=inv.id
    )
    return out


@router.post("/invoices/credit-note", response_model=PublicInvoiceOut)
async def credit_note(
    body: PublicCreditNoteIn,
    ctx: Annotated[IssuerKeyCtx, Depends(issuer_key_ctx)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> PublicInvoiceOut:
    """Create AND transmit a TD04 credit note for a parent under THIS issuer."""
    require_perm(ctx, PERM_CREDIT_NOTE)
    await _rate(ctx, "transmit")
    key = _require_idem(idempotency_key)
    await _load_invoice_for_key(ctx, body.parent_invoice_id)  # H2: parent must be ours
    req_hash = idem.request_digest(body.model_dump(mode="json"))
    claimed = await idem.claim(
        ctx.session,
        org_id=ctx.org_id,
        issuer_profile_id=ctx.issuer_profile_id,
        endpoint="credit-note",
        idempotency_key=key,
        request_hash=req_hash,
    )
    if claimed.replay is not None:
        return PublicInvoiceOut.model_validate(claimed.replay)
    assert claimed.row_id is not None  # claim row exists on every non-replay leg  # noqa: S101
    if claimed.resume_invoice_id is not None:
        # Resume the unsettled TD04 (ADR-0046) instead of creating a second
        # one; a settled TD04 is returned as-is.
        note = await _resume_transmit(ctx, claimed.resume_invoice_id)
    else:
        note = await invoice_svc.create_credit_note(
            ctx.session,
            org_id=ctx.org_id,
            actor_id=ctx.key_id,
            parent_invoice_id=body.parent_invoice_id,
            purpose=body.purpose,
        )
        # Bind the claim to the TD04 BEFORE the dispatch (retry -> resume).
        await idem.attach_invoice(ctx.session, row_id=claimed.row_id, invoice_id=note.id)
        note = await invoice_svc.transmit(
            ctx.session, org_id=ctx.org_id, actor_id=ctx.key_id, invoice_id=note.id
        )
    out = _out(note)
    await idem.store(
        ctx.session, row_id=claimed.row_id, snapshot=out.model_dump(mode="json"), invoice_id=note.id
    )
    return out


@router.get("/invoices/{invoice_id}", response_model=PublicInvoiceOut)
async def get_invoice(
    invoice_id: uuid.UUID,
    ctx: Annotated[IssuerKeyCtx, Depends(issuer_key_ctx)],
) -> PublicInvoiceOut:
    require_perm(ctx, PERM_READ)
    await _rate(ctx, "read")
    return _out(await _load_invoice_for_key(ctx, invoice_id))


@router.get("/invoices", response_model=list[PublicInvoiceOut])
async def list_invoices(
    ctx: Annotated[IssuerKeyCtx, Depends(issuer_key_ctx)],
    client_tag_id: Annotated[uuid.UUID | None, Query()] = None,
    state: Annotated[InvoiceState | None, Query()] = None,
) -> list[PublicInvoiceOut]:
    require_perm(ctx, PERM_READ)
    await _rate(ctx, "read")
    rows = await invoice_svc.list_invoices(
        ctx.session,
        org_id=ctx.org_id,
        issuer_profile_id=ctx.issuer_profile_id,  # hard issuer scope
        client_tag_id=client_tag_id,
        state=state,
    )
    return [_out(r) for r in rows]


@router.get("/events", response_model=list[PublicEventOut])
async def events(
    ctx: Annotated[IssuerKeyCtx, Depends(issuer_key_ctx)],
    since: Annotated[datetime.datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[PublicEventOut]:
    """A cursor feed of invoice state changes for this key's issuer, oldest
    first: poll with ``since`` = the previous page's last ``updated_at``."""
    require_perm(ctx, PERM_READ)
    await _rate(ctx, "read")
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=datetime.UTC)
    rows = await invoice_svc.list_invoice_changes(
        ctx.session,
        org_id=ctx.org_id,
        issuer_profile_id=ctx.issuer_profile_id,
        since=since,
        limit=limit,
    )
    return [
        PublicEventOut(
            invoice_id=r.id, state=r.state, sdi_status=r.sdi_status, updated_at=r.updated_at
        )
        for r in rows
    ]


@router.get("/invoices/{invoice_id}/notifications", response_model=list[PublicNotificationOut])
async def list_notifications(
    invoice_id: uuid.UUID,
    ctx: Annotated[IssuerKeyCtx, Depends(issuer_key_ctx)],
) -> list[PublicNotificationOut]:
    require_perm(ctx, PERM_READ)
    await _rate(ctx, "read")
    await _load_invoice_for_key(ctx, invoice_id)
    rows: list[InvoiceNotification] = await invoice_svc.list_invoice_notifications(
        ctx.session, org_id=ctx.org_id, invoice_id=invoice_id
    )
    return [PublicNotificationOut.model_validate(r) for r in rows]


@router.get("/invoices/{invoice_id}/xml")
async def download_xml(
    invoice_id: uuid.UUID,
    ctx: Annotated[IssuerKeyCtx, Depends(issuer_key_ctx)],
) -> Response:
    require_perm(ctx, PERM_DOWNLOAD)
    await _rate(ctx, "read")
    await _load_invoice_for_key(ctx, invoice_id)
    xml = await invoice_svc.get_xml_preview(ctx.session, org_id=ctx.org_id, invoice_id=invoice_id)
    return Response(content=xml, media_type="application/xml", headers=_NO_STORE)


@router.get("/invoices/{invoice_id}/pdf")
async def download_pdf(
    invoice_id: uuid.UUID,
    ctx: Annotated[IssuerKeyCtx, Depends(issuer_key_ctx)],
) -> Response:
    require_perm(ctx, PERM_DOWNLOAD)
    await _rate(ctx, "read")
    await _load_invoice_for_key(ctx, invoice_id)
    _number, pdf = await invoice_svc.render_pdf(
        ctx.session, org_id=ctx.org_id, invoice_id=invoice_id
    )
    return Response(content=pdf, media_type="application/pdf", headers=_NO_STORE)


@router.get("/invoices/{invoice_id}/notifications/{notification_id}/xml")
async def download_receipt(
    invoice_id: uuid.UUID,
    notification_id: uuid.UUID,
    ctx: Annotated[IssuerKeyCtx, Depends(issuer_key_ctx)],
) -> Response:
    require_perm(ctx, PERM_DOWNLOAD)
    await _rate(ctx, "read")
    await _load_invoice_for_key(ctx, invoice_id)
    xml, _fn = await invoice_svc.get_invoice_notification_xml(
        ctx.session, org_id=ctx.org_id, invoice_id=invoice_id, notification_id=notification_id
    )
    return Response(content=xml, media_type="application/xml", headers=_NO_STORE)
