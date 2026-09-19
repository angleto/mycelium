"""``GET /activity/watermark`` — how much has happened since an instant.

The read a live view polls so it can notice that an MCP session, the CLI or
the worker changed something under it. It answers with a count and with the
instant the SERVER measured from; it never answers with the changes
themselves, and that is the point: the caller re-reads the view through the
ordinary route, where authorisation, redaction and RLS are applied exactly
as they were on the first read. Nothing here is a second path to data.

Mapped to ``tasks:read`` because ``tasks`` is the only scope there is. A
second scope (notes, say) does NOT widen this entry: the handler would have
to require the key that matches the requested scope, since a credential that
may read notes has no business counting task activity (SEC: what a caller
can see follows what it may do).
"""

from __future__ import annotations

import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from mycelium_api.deps import TenantCtx, tenant_ctx
from mycelium_core.services import activity as svc

router = APIRouter(prefix="/activity", tags=["activity"])


class WatermarkOut(BaseModel):
    since: datetime.datetime
    changes: int


@router.get("/watermark", response_model=WatermarkOut)
async def get_watermark(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    scope: svc.WatchScope,
    # Omitted on the first call: the server then answers with the instant it
    # chose, and the count that comes back with it is the caller's BASELINE,
    # not a reason to refresh. Thereafter the caller hands the same value
    # back unchanged and watches the count move. Comparing against the
    # baseline rather than against zero is what lets the bootstrap overlap
    # backwards far enough to cover a write that was already in flight.
    since: Annotated[datetime.datetime | None, Query()] = None,
) -> WatermarkOut:
    w = await svc.watermark(ctx.session, org_id=ctx.org_id, scope=scope, since=since)
    return WatermarkOut(since=w.since, changes=w.changes)
