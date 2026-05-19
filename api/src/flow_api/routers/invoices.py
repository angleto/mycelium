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
from typing import Annotated

from fastapi import APIRouter, Depends, status

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    ConservationAdhesionIn,
    CreditNoteIn,
    InvoiceCreateIn,
    InvoiceLineIn,
    InvoiceLineOut,
    InvoiceOut,
    InvoicePatchIn,
    InvoiceXmlOut,
    IssuerProfileIn,
    IssuerProfileOut,
    IssuerProfilePatchIn,
    ReceiptIn,
    TransmitIn,
)
from flow_core.errors import NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.invoice import Invoice, InvoiceLine, IssuerProfile
from flow_core.models.membership import Role
from flow_core.services import invoice as svc
from flow_core.services.rbac import ensure_role

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
        causale=i.causale,
        notes=i.notes,
        payment_iban=i.payment_iban,
        payment_due_date=i.payment_due_date,
        taxable=i.taxable,
        vat=i.vat,
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
        natura=ln.natura,
    )


def _ip_out(p: IssuerProfile) -> IssuerProfileOut:
    return IssuerProfileOut(
        id=p.id,
        label=p.label,
        denominazione=p.denominazione,
        piva=p.piva,
        codice_fiscale=p.codice_fiscale,
        regime_fiscale=p.regime_fiscale,
        paese=p.paese,
        indirizzo=p.indirizzo,
        cap=p.cap,
        comune=p.comune,
        provincia=p.provincia,
        nazione=p.nazione,
        rea=p.rea,
        is_default=p.is_default,
        conservation_adhesion=p.conservation_adhesion.value,
        version=p.version,
    )


# --- issuer profiles (the invoice "intestazione") ---


@router.get("/issuer-profiles", response_model=list[IssuerProfileOut])
async def list_issuer_profiles(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> list[IssuerProfileOut]:
    rows = await svc.list_issuer_profiles(ctx.session, org_id=ctx.org_id)
    return [_ip_out(p) for p in rows]


@router.post("/issuer-profiles", response_model=IssuerProfileOut)
async def create_issuer_profile(
    body: IssuerProfileIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> IssuerProfileOut:
    ensure_role(ctx.role, Role.admin)
    p = await svc.create_issuer_profile(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        label=body.label,
        denominazione=body.denominazione,
        piva=body.piva,
        codice_fiscale=body.codice_fiscale,
        regime_fiscale=body.regime_fiscale,
        paese=body.paese,
        indirizzo=body.indirizzo,
        cap=body.cap,
        comune=body.comune,
        provincia=body.provincia,
        nazione=body.nazione,
        rea=body.rea,
        is_default=body.is_default,
    )
    return _ip_out(p)


@router.get("/issuer-profiles/{profile_id}", response_model=IssuerProfileOut)
async def get_issuer_profile(
    profile_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> IssuerProfileOut:
    return _ip_out(
        await svc.get_issuer_profile(ctx.session, org_id=ctx.org_id, profile_id=profile_id)
    )


@router.patch("/issuer-profiles/{profile_id}", response_model=IssuerProfileOut)
async def update_issuer_profile(
    profile_id: uuid.UUID,
    body: IssuerProfilePatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> IssuerProfileOut:
    ensure_role(ctx.role, Role.admin)
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
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> IssuerProfileOut:
    ensure_role(ctx.role, Role.admin)
    p = await svc.set_default_issuer_profile(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, profile_id=profile_id
    )
    return _ip_out(p)


@router.put("/issuer-profiles/{profile_id}/conservation", response_model=IssuerProfileOut)
async def set_conservation(
    profile_id: uuid.UUID,
    body: ConservationAdhesionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
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
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    ensure_role(ctx.role, Role.admin)
    await svc.delete_issuer_profile(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, profile_id=profile_id
    )


# --- invoices ---


@router.post("/invoices", response_model=InvoiceOut)
async def create_invoice(
    body: InvoiceCreateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> InvoiceOut:
    inv = await svc.create_draft(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        client_tag_id=body.client_tag_id,
        issuer_profile_id=body.issuer_profile_id,
        year=body.year,
        series=body.series,
        causale=body.causale,
    )
    return _inv_out(inv)


@router.patch("/invoices/{invoice_id}", response_model=InvoiceOut)
async def update_invoice(
    invoice_id: uuid.UUID,
    body: InvoicePatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
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
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
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
        natura=body.natura,
    )
    return _line_out(ln)


@router.put("/invoices/{invoice_id}/lines/{line_id}", response_model=InvoiceLineOut)
async def update_line(
    invoice_id: uuid.UUID,
    line_id: uuid.UUID,
    body: InvoiceLineIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
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
        natura=body.natura,
    )
    return _line_out(ln)


@router.delete(
    "/invoices/{invoice_id}/lines/{line_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_line(
    invoice_id: uuid.UUID,
    line_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
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
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> list[InvoiceLineOut]:
    rows = await svc.list_lines(ctx.session, org_id=ctx.org_id, invoice_id=invoice_id)
    return [_line_out(r) for r in rows]


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(
    invoice_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> InvoiceOut:
    return _inv_out(await svc.get_invoice(ctx.session, org_id=ctx.org_id, invoice_id=invoice_id))


@router.get("/invoices", response_model=list[InvoiceOut])
async def list_invoices(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> list[InvoiceOut]:
    rows = await svc.list_invoices(ctx.session, org_id=ctx.org_id)
    return [_inv_out(i) for i in rows]


@router.post("/invoices/{invoice_id}/transmit", response_model=InvoiceOut)
async def transmit(
    invoice_id: uuid.UUID,
    body: TransmitIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
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
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> InvoiceXmlOut:
    inv = await svc.get_invoice(ctx.session, org_id=ctx.org_id, invoice_id=invoice_id)
    if inv.xml is None:
        raise NotFoundError(MessageCode.INVOICE_INVALID, detail="not transmitted")
    return InvoiceXmlOut(xml=inv.xml)


@router.post("/invoices/credit-note", response_model=InvoiceOut)
async def credit_note(
    body: CreditNoteIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> InvoiceOut:
    inv = await svc.create_credit_note(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        parent_invoice_id=body.parent_invoice_id,
        causale=body.causale,
    )
    return _inv_out(inv)


@router.post("/invoices/{invoice_id}/paid", response_model=InvoiceOut)
async def mark_paid(
    invoice_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
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
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
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
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    await svc.delete_draft(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        invoice_id=invoice_id,
    )
