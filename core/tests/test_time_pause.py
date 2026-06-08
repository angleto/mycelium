"""Pause/resume for the live timer (migration 0039).

A running timer can be paused without being finalized: the active
segment is banked into ``accumulated_seconds`` and ``resumed_at`` is
cleared so the elapsed freezes; resume opens a new segment. The billed
``duration_seconds`` is the sum of active segments, excluding pause gaps.
The timer stays server-authoritative — the clock is monkeypatched here so
the banking is asserted deterministically rather than via wall-clock
sleeps.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from flow_core.db import admin_session, tenant_session
from flow_core.errors import DomainError
from flow_core.services import tasks
from flow_core.services import time_tracking as tt
from flow_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


class _Clock:
    """A controllable stand-in for ``time_tracking._now``: every service
    call reads the current value; tests advance it explicitly."""

    def __init__(self, start: dt.datetime) -> None:
        self.t = start

    def __call__(self) -> dt.datetime:
        return self.t

    def advance(self, seconds: int) -> None:
        self.t += dt.timedelta(seconds=seconds)


async def _signup_org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="PAUSE")
    return a.org_id, a.user_id


async def test_pause_banks_and_freezes_then_resume_sums(monkeypatch) -> None:
    clock = _Clock(dt.datetime(2026, 1, 12, 9, 0, tzinfo=dt.UTC))
    monkeypatch.setattr(tt, "_now", clock)
    org, user = await _signup_org()
    async with tenant_session(str(org), str(user)) as s:
        t = await tasks.create_task(s, org_id=org, actor_id=user, title="T")

        e = await tt.start_timer(s, org_id=org, actor_id=user, task_id=t.id)
        assert e.resumed_at == clock() and e.accumulated_seconds == 0

        clock.advance(600)  # 10 min of active work
        paused = await tt.pause_timer(s, org_id=org, actor_id=user, task_id=t.id)
        assert paused.ended_at is None  # still open, keeps its slot
        assert paused.resumed_at is None  # frozen
        assert paused.accumulated_seconds == 600
        # While paused it is still "running" for the listing (open row)…
        running = await tt.running_entries(s, org_id=org, user_id=user)
        assert [r.id for r in running] == [e.id]

        clock.advance(300)  # 5 min elapse *during* the pause -> not billed
        resumed = await tt.resume_timer(s, org_id=org, actor_id=user, task_id=t.id)
        assert resumed.resumed_at == clock()  # new segment opens now
        assert resumed.accumulated_seconds == 600  # unchanged by the gap

        clock.advance(120)  # 2 more min of active work
        stopped = await tt.stop_timer(s, org_id=org, actor_id=user, task_id=t.id)
        assert stopped.ended_at is not None
        assert stopped.resumed_at is None
        # 600 + 120 active; the 300s pause gap is excluded.
        assert stopped.duration_seconds == 720
        assert stopped.accumulated_seconds == 720


async def test_pause_and_resume_are_idempotent(monkeypatch) -> None:
    clock = _Clock(dt.datetime(2026, 1, 12, 9, 0, tzinfo=dt.UTC))
    monkeypatch.setattr(tt, "_now", clock)
    org, user = await _signup_org()
    async with tenant_session(str(org), str(user)) as s:
        t = await tasks.create_task(s, org_id=org, actor_id=user, title="T")
        await tt.start_timer(s, org_id=org, actor_id=user, task_id=t.id)

        clock.advance(100)
        await tt.pause_timer(s, org_id=org, actor_id=user, task_id=t.id)
        clock.advance(50)
        # Pausing an already-paused timer banks nothing more.
        p2 = await tt.pause_timer(s, org_id=org, actor_id=user, task_id=t.id)
        assert p2.resumed_at is None
        assert p2.accumulated_seconds == 100

        r1 = await tt.resume_timer(s, org_id=org, actor_id=user, task_id=t.id)
        clock.advance(40)
        # Resuming an already-running timer does not reset the segment.
        r2 = await tt.resume_timer(s, org_id=org, actor_id=user, task_id=t.id)
        assert r2.resumed_at == r1.resumed_at
        assert r2.accumulated_seconds == 100


async def test_multiple_pause_resume_cycles_sum(monkeypatch) -> None:
    clock = _Clock(dt.datetime(2026, 1, 12, 9, 0, tzinfo=dt.UTC))
    monkeypatch.setattr(tt, "_now", clock)
    org, user = await _signup_org()
    async with tenant_session(str(org), str(user)) as s:
        t = await tasks.create_task(s, org_id=org, actor_id=user, title="T")
        await tt.start_timer(s, org_id=org, actor_id=user, task_id=t.id)

        clock.advance(100)  # active
        await tt.pause_timer(s, org_id=org, actor_id=user, task_id=t.id)
        clock.advance(50)  # paused (excluded)
        await tt.resume_timer(s, org_id=org, actor_id=user, task_id=t.id)
        clock.advance(200)  # active
        await tt.pause_timer(s, org_id=org, actor_id=user, task_id=t.id)
        clock.advance(30)  # paused (excluded)
        await tt.resume_timer(s, org_id=org, actor_id=user, task_id=t.id)
        clock.advance(70)  # active
        stopped = await tt.stop_timer(s, org_id=org, actor_id=user, task_id=t.id)
        assert stopped.duration_seconds == 370  # 100 + 200 + 70


async def test_start_serial_finalizes_paused_with_banked_duration(monkeypatch) -> None:
    clock = _Clock(dt.datetime(2026, 1, 12, 9, 0, tzinfo=dt.UTC))
    monkeypatch.setattr(tt, "_now", clock)
    org, user = await _signup_org()
    async with tenant_session(str(org), str(user)) as s:
        t1 = await tasks.create_task(s, org_id=org, actor_id=user, title="T1")
        t2 = await tasks.create_task(s, org_id=org, actor_id=user, title="T2")
        e1 = await tt.start_timer(s, org_id=org, actor_id=user, task_id=t1.id)
        clock.advance(300)
        await tt.pause_timer(s, org_id=org, actor_id=user, task_id=t1.id)
        clock.advance(999)  # long pause gap, must not be billed

        # Starting a new serial timer auto-stops the open (paused) serial.
        await tt.start_timer(s, org_id=org, actor_id=user, task_id=t2.id)
        e1f = await tt.get_entry(s, org_id=org, entry_id=e1.id)
        assert e1f.ended_at is not None
        assert e1f.resumed_at is None
        assert e1f.duration_seconds == 300  # banked active time, gap excluded
        # Only the new serial timer is running now.
        running = await tt.running_entries(s, org_id=org, user_id=user)
        assert [r.task_id for r in running] == [t2.id]


async def test_pause_resume_parallel_timer_targets_task(monkeypatch) -> None:
    clock = _Clock(dt.datetime(2026, 1, 12, 9, 0, tzinfo=dt.UTC))
    monkeypatch.setattr(tt, "_now", clock)
    org, user = await _signup_org()
    async with tenant_session(str(org), str(user)) as s:
        t1 = await tasks.create_task(s, org_id=org, actor_id=user, title="serial")
        t2 = await tasks.create_task(s, org_id=org, actor_id=user, title="parallel")
        await tt.start_timer(s, org_id=org, actor_id=user, task_id=t1.id)
        await tt.start_timer(s, org_id=org, actor_id=user, task_id=t2.id, parallel=True)

        clock.advance(60)
        # Pause only the parallel one (by task); the serial keeps running.
        await tt.pause_timer(s, org_id=org, actor_id=user, task_id=t2.id)
        serial = await tt.running_for_task(s, org_id=org, user_id=user, task_id=t1.id)
        par = await tt.running_for_task(s, org_id=org, user_id=user, task_id=t2.id)
        assert serial is not None and serial.resumed_at is not None  # still ticking
        assert par is not None and par.resumed_at is None  # paused
        assert par.accumulated_seconds == 60

        clock.advance(40)  # parallel paused; serial runs
        await tt.resume_timer(s, org_id=org, actor_id=user, task_id=t2.id)
        clock.advance(10)
        stopped = await tt.stop_timer(s, org_id=org, actor_id=user, task_id=t2.id)
        assert stopped.duration_seconds == 70  # 60 + 10, the 40s gap excluded


async def test_update_interval_collapses_pause_structure(monkeypatch) -> None:
    """A manual interval correction overrides the pause banking: the
    explicit [start, end] becomes a single contiguous segment with
    duration == accumulated and no open segment."""
    clock = _Clock(dt.datetime(2026, 1, 12, 9, 0, tzinfo=dt.UTC))
    monkeypatch.setattr(tt, "_now", clock)
    org, user = await _signup_org()
    async with tenant_session(str(org), str(user)) as s:
        t = await tasks.create_task(s, org_id=org, actor_id=user, title="T")
        e = await tt.start_timer(s, org_id=org, actor_id=user, task_id=t.id)
        clock.advance(100)
        paused = await tt.pause_timer(s, org_id=org, actor_id=user, task_id=t.id)
        assert paused.accumulated_seconds == 100

        start = dt.datetime(2026, 1, 12, 9, 0, tzinfo=dt.UTC)
        end = dt.datetime(2026, 1, 12, 10, 0, tzinfo=dt.UTC)
        await tt.update_entry(
            s,
            org_id=org,
            actor_id=user,
            entry_id=e.id,
            expected_version=paused.version,
            values={"started_at": start, "ended_at": end},
        )
        fixed = await tt.get_entry(s, org_id=org, entry_id=e.id)
        assert fixed.ended_at == end
        assert fixed.duration_seconds == 3600
        assert fixed.accumulated_seconds == 3600  # collapsed to the interval
        assert fixed.resumed_at is None


async def test_pause_resume_require_an_open_timer(monkeypatch) -> None:
    clock = _Clock(dt.datetime(2026, 1, 12, 9, 0, tzinfo=dt.UTC))
    monkeypatch.setattr(tt, "_now", clock)
    org, user = await _signup_org()
    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(DomainError):
            await tt.pause_timer(s, org_id=org, actor_id=user)
        with pytest.raises(DomainError):
            await tt.resume_timer(s, org_id=org, actor_id=user)
