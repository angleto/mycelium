"""Fuel-path retention (ADR-0048, task 68052297).

The retrieval trace's only pruner used to live inside the edge-usage
fold, behind the default-off garden loop: a stock deployment accumulated
an unbounded write-only table. These tests pin the dedicated retention
service: aged traces and clicks are deleted, fresh ones are kept, the
trace window is floored at ``EDGE_USAGE_WINDOW_DAYS`` (never undercut
the aggregation), and the ``trace_backlog`` sensor sees exactly the rows
the pruner should have removed.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import func, select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.retrieval_trace import RetrievalTrace
from mycelium_core.models.search_click import SearchClick
from mycelium_core.services import fuel_retention as fuel_svc
from mycelium_core.services.auth import signup
from mycelium_core.services.edge_usage import EDGE_USAGE_WINDOW_DAYS
from mycelium_core.services.garden_health import _trace_backlog


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="FUEL")
    return r.org_id, r.user_id


def _trace(org: uuid.UUID, *, age_days: int) -> RetrievalTrace:
    return RetrievalTrace(
        org_id=org,
        items=[{"blob_id": str(uuid.uuid4()), "rank": 1}],
        is_probe=False,
        created_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=age_days),
    )


def _click(org: uuid.UUID, user: uuid.UUID, *, age_days: int) -> SearchClick:
    return SearchClick(
        org_id=org,
        user_id=user,
        query="q",
        hit_kind="note",
        hit_id=uuid.uuid4(),
        rank=1,
        result_count=3,
        is_probe=False,
        ts=dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=age_days),
    )


async def _counts(org: uuid.UUID, user: uuid.UUID) -> tuple[int, int]:
    async with tenant_session(str(org), str(user)) as s:
        traces = (await s.execute(select(func.count()).select_from(RetrievalTrace))).scalar_one()
        clicks = (await s.execute(select(func.count()).select_from(SearchClick))).scalar_one()
    return int(traces), int(clicks)


async def test_prune_deletes_aged_keeps_fresh() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        s.add(_trace(org, age_days=EDGE_USAGE_WINDOW_DAYS + 10))
        s.add(_trace(org, age_days=1))
        s.add(_click(org, user, age_days=400))
        s.add(_click(org, user, age_days=1))
    async with tenant_session(str(org), str(user)) as s:
        traces, clicks = await fuel_svc.prune(s, trace_days=90, click_days=365)
    assert traces == 1
    assert clicks == 1
    assert await _counts(org, user) == (1, 1)


async def test_trace_window_floored_at_edge_usage_window() -> None:
    """``trace_days`` below the aggregation window must not delete rows
    the edge-usage fold could still read."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        s.add(_trace(org, age_days=EDGE_USAGE_WINDOW_DAYS - 5))
    async with tenant_session(str(org), str(user)) as s:
        traces, _clicks = await fuel_svc.prune(s, trace_days=1, click_days=365)
    assert traces == 0
    assert (await _counts(org, user))[0] == 1


async def test_trace_backlog_sensor_counts_prunable_rows() -> None:
    """The garden sensor reads exactly the backlog the pruner removes:
    non-zero before the sweep, zero after."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        s.add(_trace(org, age_days=EDGE_USAGE_WINDOW_DAYS + 30))
        s.add(_trace(org, age_days=1))
    now = dt.datetime.now(tz=dt.UTC)
    async with tenant_session(str(org), str(user)) as s:
        assert await _trace_backlog(s, org_id=org, now=now) == 1.0
        await fuel_svc.prune(s, trace_days=90, click_days=365)
        assert await _trace_backlog(s, org_id=org, now=now) == 0.0
