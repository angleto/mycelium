"""Search-click capture + recall_at_k sensor (ADR-0035, task 89508ca9).

The write half (``services.search_clicks.log_click``: validation, RLS
isolation) and the read half (``garden_health._recall_at_k`` through
``compute_health``: probe exclusion, top-K window, empty-denominator
semantics) against the real DB.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from flow_core.db import admin_session, tenant_session
from flow_core.errors import DomainError
from flow_core.models.search_click import SearchClick
from flow_core.services import garden_health as health_svc
from flow_core.services import search_clicks as svc
from flow_core.services.auth import signup


async def _org_user() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="SC",
        )
    return a.org_id, a.user_id


async def _click(
    s,
    org: uuid.UUID,
    user: uuid.UUID,
    *,
    rank: int,
    result_count: int = 10,
    is_probe: bool = False,
    query: str = "come si pota il melo",
) -> SearchClick:
    return await svc.log_click(
        s,
        org_id=org,
        actor_id=user,
        query=query,
        hit_kind="note",
        hit_id=uuid.uuid4(),
        rank=rank,
        result_count=result_count,
        is_probe=is_probe,
    )


async def test_log_click_persists_event() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        row = await _click(s, org, user, rank=2)
        assert row.id is not None
        got = (await s.execute(select(SearchClick).where(SearchClick.id == row.id))).scalar_one()
        assert got.query == "come si pota il melo"
        assert got.hit_kind == "note"
        assert got.rank == 2 and got.result_count == 10
        assert got.is_probe is False


async def test_log_click_rejects_bad_input() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(DomainError):
            await _click(s, org, user, rank=0)
        with pytest.raises(DomainError):
            # Clicked rank below the shown count is incoherent.
            await _click(s, org, user, rank=5, result_count=3)
        with pytest.raises(DomainError):
            await _click(s, org, user, rank=1, query="   ")
        with pytest.raises(DomainError):
            await svc.log_click(
                s,
                org_id=org,
                actor_id=user,
                query="q",
                hit_kind="banana",
                hit_id=uuid.uuid4(),
                rank=1,
                result_count=1,
            )


async def test_log_click_truncates_long_query() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        row = await _click(s, org, user, rank=1, query="x" * 2000)
        assert len(row.query) == 500


async def test_clicks_are_org_isolated() -> None:
    org_a, user_a = await _org_user()
    org_b, user_b = await _org_user()
    async with tenant_session(str(org_a), str(user_a)) as s:
        await _click(s, org_a, user_a, rank=1)
    async with tenant_session(str(org_b), str(user_b)) as s:
        rows = (await s.execute(select(SearchClick))).scalars().all()
        assert rows == []


async def test_recall_at_k_counts_top1_over_clicks_in_window() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        # 3 real clicks inside top-K: two at rank 1, one at rank 4.
        await _click(s, org, user, rank=1)
        await _click(s, org, user, rank=1)
        await _click(s, org, user, rank=4)
        # A probe click at rank 1 must NOT inflate the numerator.
        await _click(s, org, user, rank=1, is_probe=True)
        # A click below the top-K window is out of the denominator.
        await _click(s, org, user, rank=health_svc.RECALL_K + 1, result_count=50)
        health = await health_svc.compute_health(s, org_id=org)
        assert health.recall_at_k.value == pytest.approx(2 / 3, abs=1e-4)
        assert health.recall_at_k.reason is None


async def test_recall_at_k_none_without_real_clicks() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        # Probe-only traffic: the sensor must stay "no signal", not 1.0.
        await _click(s, org, user, rank=1, is_probe=True)
        health = await health_svc.compute_health(s, org_id=org)
        assert health.recall_at_k.value is None
        assert health.recall_at_k.reason is not None
        assert "probe" in health.recall_at_k.reason
