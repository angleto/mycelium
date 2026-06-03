"""Received-invoices router (passive cycle, ADR-0011 v1.1).

The receiver-cycle ingest (a FatturaElettronica SdI delivers to our
codice destinatario) writes a ``ReceivedInvoice`` row -- there is no
draft state, the row is created by the SOAP inbound. This router exposes
the buyer-side action a tenant can take on it: sending an
EsitoCommittente (EC01 accepted / EC02 rejected) back to SdI.

The signed XML is built and persisted server-side via
``services.esito_committente``; the client only declares intent.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import EsitoCommittenteIn, EsitoCommittenteOut
from flow_core.services.esito_committente import send_esito_committente

router = APIRouter(tags=["received-invoices"])


@router.post(
    "/received-invoices/{received_invoice_id}/esito-committente",
    response_model=EsitoCommittenteOut,
)
async def send_ec(
    received_invoice_id: uuid.UUID,
    body: EsitoCommittenteIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> EsitoCommittenteOut:
    notif = await send_esito_committente(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        received_invoice_id=received_invoice_id,
        esito=body.esito,
        descrizione=body.descrizione,
    )
    return EsitoCommittenteOut(
        received_invoice_id=notif.received_invoice_id,
        esito=body.esito,
        message_id=notif.message_id or "",
        sent_at=notif.received_at,
    )
