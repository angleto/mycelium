"""Date-only due-date promotion (the core service owns it, anchoring a
bare date to end-of-day in the OWNER's configured timezone -- one source
of truth across SPA / MCP / API) and the per-user reminder profile
(timezone + day_start_minute) validation / partial-update semantics.
"""

from __future__ import annotations

import datetime as dt
import uuid
from zoneinfo import ZoneInfo

import pytest

from flow_core.db import admin_session, tenant_session
from flow_core.errors import DomainError
from flow_core.services import tasks as tasks_svc
from flow_core.services import users as users_svc
from flow_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="DUE")
    return r.org_id, r.user_id


async def test_create_promotes_date_only_to_end_of_day_in_owner_tz() -> None:
    """A bare ``date`` is anchored to 23:59:59 in the OWNER's configured
    timezone -- not UTC, and not midnight."""
    org, user = await _org()
    rome = ZoneInfo("Europe/Rome")
    async with admin_session() as s:
        await users_svc.set_timezone(s, user_id=user, timezone="Europe/Rome")
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="t", due_date=dt.date(2026, 6, 10)
        )
        stored = task.due_date
    assert stored is not None
    local = stored.astimezone(rome)
    assert (local.year, local.month, local.day) == (2026, 6, 10)
    assert (local.hour, local.minute, local.second) == (23, 59, 59)
    # And NOT 23:59:59 in UTC (the old MCP behaviour for a non-UTC user).
    assert stored.astimezone(dt.UTC).hour != 23


async def test_create_keeps_explicit_datetime_as_is() -> None:
    """A real ``datetime`` is an explicit instant, stored unchanged."""
    org, user = await _org()
    when = dt.datetime(2026, 6, 10, 14, 30, tzinfo=dt.UTC)
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="t", due_date=when)
    assert task.due_date == when


async def test_update_promotes_date_only_in_owner_tz() -> None:
    org, user = await _org()
    rome = ZoneInfo("Europe/Rome")
    async with admin_session() as s:
        await users_svc.set_timezone(s, user_id=user, timezone="Europe/Rome")
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="t")
        await tasks_svc.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=task.id,
            expected_version=task.version,
            values={"due_date": dt.date(2026, 6, 10)},
        )
        refreshed = await tasks_svc.get_task(s, org_id=org, task_id=task.id)
    assert refreshed.due_date is not None
    local = refreshed.due_date.astimezone(rome)
    assert (local.hour, local.minute, local.second) == (23, 59, 59)
    assert local.date() == dt.date(2026, 6, 10)


def test_normalize_day_start_minute_valid() -> None:
    assert users_svc.normalize_day_start_minute(None) == 0
    assert users_svc.normalize_day_start_minute(0) == 0
    assert users_svc.normalize_day_start_minute(360) == 360
    assert users_svc.normalize_day_start_minute(1439) == 1439


@pytest.mark.parametrize("bad", [-1, 1440, 5000])
def test_normalize_day_start_minute_rejects_out_of_range(bad: int) -> None:
    with pytest.raises(DomainError):
        users_svc.normalize_day_start_minute(bad)


async def test_update_profile_applies_only_provided_fields() -> None:
    """Patching the day start must not clear the timezone, and vice-versa
    (the _UNSET sentinel distinguishes 'leave alone' from 'set to None')."""
    _, user = await _org()
    async with admin_session() as s:
        await users_svc.update_profile(
            s, user_id=user, timezone="Europe/Rome", day_start_minute=360
        )
    async with admin_session() as s:
        u = await users_svc.update_profile(s, user_id=user, day_start_minute=420)
        assert u.timezone == "Europe/Rome"  # untouched
        assert u.day_start_minute == 420
    async with admin_session() as s:
        u = await users_svc.update_profile(s, user_id=user, timezone="America/New_York")
        assert u.day_start_minute == 420  # untouched
        assert u.timezone == "America/New_York"
