"""Retention for the fuel-path telemetry tables (ADR-0048, task 68052297).

The "fuel path" is the set of append-mostly telemetry tables the memory
system writes on every use and only ever consumes via aggregation:
``retrieval_trace`` (one content-free row per non-probe search, the raw
signal of the edge-usage fold) and ``search_clicks`` (the recall@k
sensor's click log). Neither had a retention path outside the fold --
and the fold rides the default-off garden sweep, so a stock deployment
accumulated an unbounded write-only ``retrieval_trace``.

This service prunes both on fixed windows, called by the dedicated
``fuel_retention`` worker job which runs UNCONDITIONALLY (hygiene, not
metabolism: it must not depend on ``garden_loop_enabled``). Safety
invariant: the trace window is floored at ``EDGE_USAGE_WINDOW_DAYS`` --
the fold only ever reads traces inside that window ("aged-out traces can
never contribute again", services.edge_usage), so this job deletes only
rows the aggregation could never use, whether or not the fold ever runs.

``activity_log`` is deliberately NOT pruned here: it is the append-only
audit spine (coactivity input, garden timeline, accountability) and its
append-only stance is a recorded decision (ADR-0048), not an omission.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.models.retrieval_trace import RetrievalTrace
from mycelium_core.models.search_click import SearchClick
from mycelium_core.services.edge_usage import EDGE_USAGE_WINDOW_DAYS


async def prune(
    session: AsyncSession,
    *,
    trace_days: int,
    click_days: int,
    now: dt.datetime | None = None,
) -> tuple[int, int]:
    """Delete aged fuel rows for the current tenant (RLS-scoped).

    Returns ``(traces_deleted, clicks_deleted)``. ``trace_days`` is
    floored at ``EDGE_USAGE_WINDOW_DAYS`` so retention can be raised but
    never undercut the edge-usage aggregation window.
    """
    ref = now or dt.datetime.now(tz=dt.UTC)
    trace_cutoff = ref - dt.timedelta(days=max(trace_days, EDGE_USAGE_WINDOW_DAYS))
    click_cutoff = ref - dt.timedelta(days=click_days)
    traces_res = await session.execute(
        delete(RetrievalTrace).where(RetrievalTrace.created_at < trace_cutoff)
    )
    clicks_res = await session.execute(delete(SearchClick).where(SearchClick.ts < click_cutoff))
    await session.flush()
    traces = int(getattr(traces_res, "rowcount", 0) or 0)
    clicks = int(getattr(clicks_res, "rowcount", 0) or 0)
    return traces, clicks
