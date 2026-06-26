"""Invoicing + issuer-profile router. Thin adapter (docs/adr/0001,
0009, 0010, 0011, FR-9). Immutability, numbering and conservation are
enforced in the service; the SdI channel is injected (manual export by
default, fake SdICoop in tests).

A draft is fully editable (invoice-level fields + lines); the chosen
issuer profile (the "intestazione") is one of the org's profiles, the
default pre-selected. After transmission the document is append-only:
the only correction is a TD04 credit note."""

from __future__ import annotations

import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from mycelium_api.deps import TenantCtx, tenant_ctx
from mycelium_api.schemas import (
    ConservationAdhesionIn,
    CreditNoteIn,
    InvoiceCounterOut,
    InvoiceCounterPatchIn,
    InvoiceCreateIn,
    InvoiceLineIn,
    InvoiceLineOut,
    InvoiceOut,
    InvoicePatchIn,
    InvoicePreviewLine,
    InvoicePreviewOut,
    InvoicePreviewParty,
    InvoicePreviewTotals,
    InvoiceXmlOut,
    IssuerProfileIn,
    IssuerProfileOut,
    IssuerProfilePatchIn,
    ReceiptIn,
    SdiMandateIn,
    SdiMandateOut,
    TransmitIn,
)
from mycelium_core.models.invoice import Invoice, InvoiceLine, IssuerProfile
from mycelium_core.models.membership import Role
from mycelium_core.models.sdi_mandate import SdiMandate
from mycelium_core.services import invoice as svc
from mycelium_core.services import sdi_mandate as msvc
from mycelium_core.services.rbac import ensure_role

router = APIRouter(tags=["invoices"])


def _inv_out(i: Invoice) -> InvoiceOut:
    return InvoiceOut(
        id=i.id,
        client_tag_id=i.client_tag_id,
        issuer_profile_id=i.issuer_profile_id,
        kind=i.kind,
        document_type=i.document_type,
        parent_invoice_id=i.parent_invoice_id,
        series=i.series,
        year=i.year,
        number=i.number,
        state=i.state,
        currency=i.currency,
        purpose=i.purpose,
        notes=i.notes,
        payment_iban=i.payment_iban,
        payment_due_date=i.payment_due_date,
        payment_conditions_code=i.payment_conditions_code,
        payment_method_code=i.payment_method_code,
        payment_terms_days=i.payment_terms_days,
        taxable=i.taxable,
        vat=i.vat,
        stamp_duty=i.stamp_duty,
        total=i.total,
        identificativo_sdi=i.identificativo_sdi,
        sdi_status=i.sdi_status,
        payment_status=i.payment_status,
        conservation_status=i.conservation_status,
        version=i.version,
    )


def _line_out(ln: InvoiceLine) -> InvoiceLineOut:
    return InvoiceLineOut(
        id=ln.id,
        line_no=ln.line_no,
        description=ln.description,
        quantity=ln.quantity,
        unit_price=ln.unit_price,
        vat_rate=ln.vat_rate,
        vat_nature=ln.vat_nature,
    )


def _ip_out(p: IssuerProfile) -> IssuerProfileOut:
    return IssuerProfileOut(
        id=p.id,
        label=p.label,
        legal_name=p.legal_name,
        vat_number=p.vat_number,
        tax_code=p.tax_code,
        tax_regime=p.tax_regime,
        country_code=p.country_code,
        address=p.address,
        postal_code=p.postal_code,
        city=p.city,
        province=p.province,
        country=p.country,
        sdi_code=p.sdi_code,
        rea=p.rea,
        default_iban=p.default_iban,
        legal_reference=p.legal_reference,
        first_name=p.first_name,
        last_name=p.last_name,
        pec=p.pec,
        email=p.email,
        phone=p.phone,
        fax=p.fax,
        default_payment_conditions_code=p.default_payment_conditions_code,
        default_payment_method_code=p.default_payment_method_code,
        default_payment_terms_days=p.default_payment_terms_days,
        is_default=p.is_default,
        conservation_adhesion=p.conservation_adhesion.value,
        version=p.version,
    )


def _mandate_out(m: SdiMandate) -> SdiMandateOut:
    return SdiMandateOut(
        id=m.id,
        issuer_profile_id=m.issuer_profile_id,
        status=m.status.value,
        scope=m.scope,
        reference=m.reference,
        granted_at=m.granted_at,
        revoked_at=m.revoked_at,
        version=m.version,
    )


# --- issuer profiles (the invoice "intestazione") ---


@router.get("/issuer-profiles", response_model=list[IssuerProfileOut])
async def list_issuer_profiles(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[IssuerProfileOut]:
    rows = await svc.list_issuer_profiles(ctx.session, org_id=ctx.org_id)
    return [_ip_out(p) for p in rows]


@router.post("/issuer-profiles", response_model=IssuerProfileOut)
async def create_issuer_profile(
    body: IssuerProfileIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> IssuerProfileOut:
    ensure_role(ctx.role, Role.owner)
    p = await svc.create_issuer_profile(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        label=body.label,
        legal_name=body.legal_name,
        vat_number=body.vat_number,
        tax_code=body.tax_code,
        tax_regime=body.tax_regime,
        country_code=body.country_code,
        address=body.address,
        postal_code=body.postal_code,
        city=body.city,
        province=body.province,
        country=body.country,
        sdi_code=body.sdi_code,
        rea=body.rea,
        default_iban=body.default_iban,
        legal_reference=body.legal_reference,
        first_name=body.first_name,
        last_name=body.last_name,
        pec=body.pec,
        email=body.email,
        phone=body.phone,
        fax=body.fax,
        default_payment_conditions_code=body.default_payment_conditions_code,
        default_payment_method_code=body.default_payment_method_code,
        default_payment_terms_days=body.default_payment_terms_days,
        is_default=body.is_default,
    )
    return _ip_out(p)


@router.get("/issuer-profiles/{profile_id}", response_model=IssuerProfileOut)
async def get_issuer_profile(
    profile_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> IssuerProfileOut:
    return _ip_out(
        await svc.get_issuer_profile(ctx.session, org_id=ctx.org_id, profile_id=profile_id)
    )


@router.patch("/issuer-profiles/{profile_id}", response_model=IssuerProfileOut)
async def update_issuer_profile(
    profile_id: uuid.UUID,
    body: IssuerProfilePatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> IssuerProfileOut:
    ensure_role(ctx.role, Role.owner)
    values = body.model_dump(exclude_unset=True, exclude={"is_default"})
    p = await svc.update_issuer_profile(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        profile_id=profile_id,
        values=values,
        is_default=body.is_default,
    )
    return _ip_out(p)


@router.post("/issuer-profiles/{profile_id}/default", response_model=IssuerProfileOut)
async def set_default_issuer_profile(
    profile_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> IssuerProfileOut:
    ensure_role(ctx.role, Role.owner)
    p = await svc.set_default_issuer_profile(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, profile_id=profile_id
    )
    return _ip_out(p)


@router.put("/issuer-profiles/{profile_id}/conservation", response_model=IssuerProfileOut)
async def set_conservation(
    profile_id: uuid.UUID,
    body: ConservationAdhesionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> IssuerProfileOut:
    p = await svc.set_conservation_adhesion(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        profile_id=profile_id,
        adhesion=body.adhesion,
    )
    return _ip_out(p)


@router.delete("/issuer-profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_issuer_profile(
    profile_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    ensure_role(ctx.role, Role.owner)
    await svc.delete_issuer_profile(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, profile_id=profile_id
    )


# --- invoice counter override (migration from another billing system) ---


@router.get(
    "/issuer-profiles/{profile_id}/counters",
    response_model=list[InvoiceCounterOut],
)
async def list_counters(
    profile_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[InvoiceCounterOut]:
    """Counters owned by this issuer, with ``max_emitted`` as the lower
    bound for any override. The UI uses it to disable an out-of-range
    input before the user hits Save."""
    rows = await svc.list_counters(ctx.session, org_id=ctx.org_id, issuer_profile_id=profile_id)
    return [
        InvoiceCounterOut(
            issuer_profile_id=r.issuer_profile_id,
            series=r.series,
            year=r.year,
            last_number=r.last_number,
            max_emitted=r.max_emitted,
        )
        for r in rows
    ]


@router.put(
    "/issuer-profiles/{profile_id}/counters/{series}/{year}",
    response_model=InvoiceCounterOut,
)
async def set_counter(
    profile_id: uuid.UUID,
    series: str,
    year: int,
    body: InvoiceCounterPatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> InvoiceCounterOut:
    """Override the next number for (issuer, series, year). Admin only.
    The service rejects any value below ``max(invoices.number)`` for
    the same key with a conflict error."""
    ensure_role(ctx.role, Role.owner)
    r = await svc.set_counter(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        issuer_profile_id=profile_id,
        series=series,
        year=year,
        last_number=body.last_number,
    )
    return InvoiceCounterOut(
        issuer_profile_id=r.issuer_profile_id,
        series=r.series,
        year=r.year,
        last_number=r.last_number,
        max_emitted=r.max_emitted,
    )


# --- SdI transmission mandate (per issuer profile / VAT subject; ADR-0011) ---


@router.get("/issuer-profiles/{profile_id}/mandate", response_model=SdiMandateOut | None)
async def get_mandate(
    profile_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> SdiMandateOut | None:
    m = await msvc.get_active_mandate(ctx.session, org_id=ctx.org_id, issuer_profile_id=profile_id)
    return _mandate_out(m) if m is not None else None


@router.get("/issuer-profiles/{profile_id}/mandates", response_model=list[SdiMandateOut])
async def list_mandates(
    profile_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[SdiMandateOut]:
    rows = await msvc.list_mandates(ctx.session, org_id=ctx.org_id, issuer_profile_id=profile_id)
    return [_mandate_out(m) for m in rows]


@router.post("/issuer-profiles/{profile_id}/mandate", response_model=SdiMandateOut)
async def grant_mandate(
    profile_id: uuid.UUID,
    body: SdiMandateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> SdiMandateOut:
    ensure_role(ctx.role, Role.owner)
    m = await msvc.grant_mandate(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        issuer_profile_id=profile_id,
        reference=body.reference,
    )
    return _mandate_out(m)


@router.delete("/issuer-profiles/{profile_id}/mandate", response_model=SdiMandateOut)
async def revoke_mandate(
    profile_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> SdiMandateOut:
    ensure_role(ctx.role, Role.owner)
    m = await msvc.revoke_mandate(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, issuer_profile_id=profile_id
    )
    return _mandate_out(m)


# --- invoices ---


@router.post("/invoices", response_model=InvoiceOut)
async def create_invoice(
    body: InvoiceCreateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> InvoiceOut:
    inv = await svc.create_draft(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        client_tag_id=body.client_tag_id,
        issuer_profile_id=body.issuer_profile_id,
        year=body.year,
        series=body.series,
        purpose=body.purpose,
    )
    return _inv_out(inv)


@router.patch("/invoices/{invoice_id}", response_model=InvoiceOut)
async def update_invoice(
    invoice_id: uuid.UUID,
    body: InvoicePatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> InvoiceOut:
    values = body.model_dump(exclude_unset=True)
    inv = await svc.update_draft(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        invoice_id=invoice_id,
        values=values,
    )
    return _inv_out(inv)


@router.post("/invoices/{invoice_id}/lines", response_model=InvoiceLineOut)
async def add_line(
    invoice_id: uuid.UUID,
    body: InvoiceLineIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> InvoiceLineOut:
    ln = await svc.add_line(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        invoice_id=invoice_id,
        description=body.description,
        unit_price=body.unit_price,
        quantity=body.quantity,
        vat_rate=body.vat_rate,
        vat_nature=body.vat_nature,
    )
    return _line_out(ln)


@router.put("/invoices/{invoice_id}/lines/{line_id}", response_model=InvoiceLineOut)
async def update_line(
    invoice_id: uuid.UUID,
    line_id: uuid.UUID,
    body: InvoiceLineIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> InvoiceLineOut:
    ln = await svc.update_line(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        invoice_id=invoice_id,
        line_id=line_id,
        description=body.description,
        unit_price=body.unit_price,
        quantity=body.quantity,
        vat_rate=body.vat_rate,
        vat_nature=body.vat_nature,
    )
    return _line_out(ln)


@router.delete(
    "/invoices/{invoice_id}/lines/{line_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_line(
    invoice_id: uuid.UUID,
    line_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    await svc.delete_line(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        invoice_id=invoice_id,
        line_id=line_id,
    )


@router.get("/invoices/{invoice_id}/lines", response_model=list[InvoiceLineOut])
async def list_lines(
    invoice_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[InvoiceLineOut]:
    rows = await svc.list_lines(ctx.session, org_id=ctx.org_id, invoice_id=invoice_id)
    return [_line_out(r) for r in rows]


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(
    invoice_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> InvoiceOut:
    return _inv_out(await svc.get_invoice(ctx.session, org_id=ctx.org_id, invoice_id=invoice_id))


@router.get("/invoices", response_model=list[InvoiceOut])
async def list_invoices(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[InvoiceOut]:
    rows = await svc.list_invoices(ctx.session, org_id=ctx.org_id)
    return [_inv_out(i) for i in rows]


@router.post("/invoices/{invoice_id}/transmit", response_model=InvoiceOut)
async def transmit(
    invoice_id: uuid.UUID,
    body: TransmitIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> InvoiceOut:
    inv = await svc.transmit(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        invoice_id=invoice_id,
        progressivo=body.progressivo,
    )
    return _inv_out(inv)


@router.get("/invoices/{invoice_id}/xml", response_model=InvoiceXmlOut)
async def get_xml(
    invoice_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> InvoiceXmlOut:
    # Transmitted -> the frozen transited XML; draft -> a LIVE preview
    # built from the current draft (never 404 for a valid draft). The
    # service validates first, so a draft missing fiscal data raises the
    # domain error (the UI shows exactly what is missing).
    xml = await svc.get_xml_preview(ctx.session, org_id=ctx.org_id, invoice_id=invoice_id)
    return InvoiceXmlOut(xml=xml)


@router.get("/invoices/{invoice_id}/pdf")
async def get_pdf(
    invoice_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> Response:
    number, pdf = await svc.render_pdf(ctx.session, org_id=ctx.org_id, invoice_id=invoice_id)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{number}.pdf"'},
    )


def _party_out(
    legal_name: str,
    *,
    vat_number: str | None = None,
    tax_code: str | None = None,
    tax_regime: str | None = None,
    address: str | None = None,
    postal_code: str | None = None,
    city: str | None = None,
    province: str | None = None,
    country: str | None = None,
    sdi_code: str | None = None,
    pec: str | None = None,
) -> InvoicePreviewParty:
    return InvoicePreviewParty(
        legal_name=legal_name,
        vat_number=vat_number,
        tax_code=tax_code,
        tax_regime=tax_regime,
        address=address,
        postal_code=postal_code,
        city=city,
        province=province,
        country=country,
        sdi_code=sdi_code,
        pec=pec,
    )


@router.get("/invoices/{invoice_id}/preview", response_model=InvoicePreviewOut)
async def get_preview(
    invoice_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> InvoicePreviewOut:
    import datetime as _dt

    inv, p = await svc.get_preview(ctx.session, org_id=ctx.org_id, invoice_id=invoice_id)
    issuer = (
        _party_out(
            p.issuer.legal_name,
            vat_number=p.issuer.vat_number,
            tax_code=p.issuer.tax_code,
            tax_regime=p.issuer.tax_regime,
            address=p.issuer.address,
            postal_code=p.issuer.postal_code,
            city=p.issuer.city,
            province=p.issuer.province,
            country=p.issuer.country,
        )
        if p.issuer is not None
        else None
    )
    client = (
        _party_out(
            p.client.legal_name,
            vat_number=p.client.vat_number,
            tax_code=p.client.tax_code,
            address=p.client.address,
            postal_code=p.client.postal_code,
            city=p.client.city,
            province=p.client.province,
            country=p.client.country,
            sdi_code=p.client.sdi_code,
            pec=p.client.pec,
        )
        if p.client is not None
        else None
    )
    return InvoicePreviewOut(
        number=p.number,
        series=inv.series,
        year=inv.year,
        document_type=inv.document_type,
        date=(inv.issued_at or _dt.datetime.now(tz=_dt.UTC)).date(),
        payment_due_date=inv.payment_due_date,
        issuer=issuer,
        client=client,
        lines=[
            InvoicePreviewLine(
                line_no=ln.line_no,
                description=ln.description,
                quantity=ln.quantity,
                unit_price=ln.unit_price,
                line_total=(ln.quantity * ln.unit_price).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                ),
                vat_rate=ln.vat_rate,
                vat_nature=ln.vat_nature,
            )
            for ln in p.lines
        ],
        totals=InvoicePreviewTotals(
            taxable=p.totals.taxable,
            vat=p.totals.vat,
            stamp_duty=p.totals.stamp_duty,
            total=p.totals.total,
        ),
        effective_iban=p.effective_iban,
        iban_source=p.iban_source,
        purpose=inv.purpose,
        notes=inv.notes,
        is_forfettario=p.is_forfettario,
        state=inv.state,
        identificativo_sdi=inv.identificativo_sdi,
        sdi_status=inv.sdi_status,
        conservation_status=inv.conservation_status,
    )


@router.post("/invoices/credit-note", response_model=InvoiceOut)
async def credit_note(
    body: CreditNoteIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> InvoiceOut:
    inv = await svc.create_credit_note(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        parent_invoice_id=body.parent_invoice_id,
        purpose=body.purpose,
    )
    return _inv_out(inv)


@router.post("/invoices/{invoice_id}/paid", response_model=InvoiceOut)
async def mark_paid(
    invoice_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> InvoiceOut:
    inv = await svc.mark_paid(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        invoice_id=invoice_id,
    )
    return _inv_out(inv)


@router.post("/invoices/receipt", response_model=InvoiceOut)
async def ingest_receipt(
    body: ReceiptIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> InvoiceOut:
    inv = await svc.ingest_receipt(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        identificativo_sdi=body.identificativo_sdi,
        outcome=body.outcome,
    )
    return _inv_out(inv)


@router.delete("/invoices/{invoice_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_draft(
    invoice_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    await svc.delete_draft(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        invoice_id=invoice_id,
    )
