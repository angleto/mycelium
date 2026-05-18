"""Invoicing + fiscal-profile router. Thin adapter (docs/adr/0001,
0009, 0010, 0011, FR-9). Immutability, numbering and conservation are
enforced in the service; the SdI channel is injected (manual export by
default, fake SdICoop in tests)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    ConservationAdhesionIn,
    CreditNoteIn,
    FiscalProfileIn,
    FiscalProfileOut,
    InvoiceCreateIn,
    InvoiceLineIn,
    InvoiceLineOut,
    InvoiceOut,
    InvoiceXmlOut,
    ReceiptIn,
    TransmitIn,
)
from flow_core.errors import NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.invoice import Invoice, InvoiceLine, OrgFiscalProfile
from flow_core.services import invoice as svc

router = APIRouter(tags=["invoices"])


def _inv_out(i: Invoice) -> InvoiceOut:
    return InvoiceOut(
        id=i.id,
        client_tag_id=i.client_tag_id,
        kind=i.kind,
        document_type=i.document_type,
        parent_invoice_id=i.parent_invoice_id,
        series=i.series,
        year=i.year,
        number=i.number,
        state=i.state,
        currency=i.currency,
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


def _fp_out(p: OrgFiscalProfile) -> FiscalProfileOut:
    return FiscalProfileOut(
        denominazione=p.denominazione,
        piva=p.piva,
        codice_fiscale=p.codice_fiscale,
        regime_fiscale=p.regime_fiscale,
        conservation_adhesion=p.conservation_adhesion.value,
        version=p.version,
    )


@router.put("/fiscal-profile", response_model=FiscalProfileOut)
async def set_fiscal_profile(
    body: FiscalProfileIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> FiscalProfileOut:
    p = await svc.upsert_fiscal_profile(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
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
    )
    return _fp_out(p)


@router.get("/fiscal-profile", response_model=FiscalProfileOut)
async def get_fiscal_profile(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> FiscalProfileOut:
    p = await svc.get_fiscal_profile(ctx.session, org_id=ctx.org_id)
    if p is None:
        raise NotFoundError(MessageCode.FISCAL_PROFILE_REQUIRED, detail="missing")
    return _fp_out(p)


@router.put("/fiscal-profile/conservation", response_model=FiscalProfileOut)
async def set_conservation(
    body: ConservationAdhesionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> FiscalProfileOut:
    p = await svc.set_conservation_adhesion(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        adhesion=body.adhesion,
    )
    return _fp_out(p)


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
        year=body.year,
        series=body.series,
        causale=body.causale,
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
