"""ADR-0037 follow-up (task ea2156df): prior snapshot, point-in-time
rollback, and the reject-hotspot / drift telemetry.

Covers the gap rebuild_from_feedback can't close — decay-aware
point-in-time recovery — plus the read-only learning telemetry. All
against the real DB, mirroring test_garden_learning.py.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy import select, update

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.classification_feedback import ClassificationFeedback
from mycelium_core.models.classification_personal_prior import ClassificationPersonalPrior as _Prior
from mycelium_core.models.classification_personal_prior_snapshot import (
    ClassificationPersonalPriorSnapshot as _Snapshot,
)
from mycelium_core.services import garden_learning as learn
from mycelium_core.services.auth import signup


async def _org_user(name: str = "GLR") -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name=name,
        )
    return a.org_id, a.user_id


def _fb(
    s: object,
    *,
    org: uuid.UUID,
    user: uuid.UUID,
    tag_id: str,
    action: str,
    ts: datetime.datetime,
) -> None:
    """Insert a controlled tag-suggestion feedback row."""
    s.add(  # type: ignore[attr-defined]
        ClassificationFeedback(
            org_id=org,
            user_id=user,
            node_id=uuid.uuid4(),
            suggestion_type="tag",
            suggestion_value={"tag_id": tag_id},
            action=action,
            ts=ts,
            model_version="test",
        )
    )


async def _live(s: object, org: uuid.UUID, user: uuid.UUID) -> dict[str, float]:
    rows = (
        await s.execute(  # type: ignore[attr-defined]
            select(_Prior.feature_key, _Prior.value).where(
                _Prior.org_id == org, _Prior.user_id == user
            )
        )
    ).all()
    return {k: float(v) for k, v in rows}


# --- snapshot ----------------------------------------------------------


async def test_snapshot_writes_blob_per_user_and_is_daily_idempotent() -> None:
    org, user = await _org_user()
    now = datetime.datetime.now(datetime.UTC)
    async with tenant_session(str(org), str(user)) as s:
        await learn.record_decision(
            s,
            org_id=org,
            user_id=user,
            suggestion_type="tag",
            suggestion_value={"tag_id": "T1"},
            action="accept",
        )
        # First snapshot writes one row; the blob mirrors the live priors.
        assert await learn.snapshot_priors(s, org_id=org, now=now) == 1
        snap = (await s.execute(select(_Snapshot).where(_Snapshot.org_id == org))).scalar_one()
        assert snap.blob == await _live(s, org, user)
        # Same day -> skipped (daily-idempotent under a minutes-cadence sweep).
        assert (
            await learn.snapshot_priors(s, org_id=org, now=now + datetime.timedelta(hours=2)) == 0
        )
        # > SNAPSHOT_MIN_INTERVAL later -> a fresh checkpoint.
        assert (
            await learn.snapshot_priors(
                s, org_id=org, now=now + learn.SNAPSHOT_MIN_INTERVAL + datetime.timedelta(minutes=1)
            )
            == 1
        )


# --- rollback ----------------------------------------------------------


async def test_rollback_restores_snapshot_base_decay_aware_not_pure_rebuild() -> None:
    """The headline property: rollback restores the *snapshot* value (which
    carries the decay applied before it), where a pure rebuild-from-log
    would recompute the undecayed value."""
    org, user = await _org_user()
    t0 = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=10)
    async with tenant_session(str(org), str(user)) as s:
        _fb(s, org=org, user=user, tag_id="X", action="accept", ts=t0)
        await s.flush()
        # Live prior from the log = the undecayed value v0.
        await learn.rebuild_from_feedback(s, org_id=org, user_id=user)
        v0 = (await _live(s, org, user))["tag:X"]
        # Simulate a nightly decay having shrunk it, then checkpoint THAT.
        v_dec = v0 * 0.5
        await s.execute(
            update(_Prior)
            .where(_Prior.org_id == org, _Prior.user_id == user, _Prior.feature_key == "tag:X")
            .values(value=v_dec)
        )
        t_snap = t0 + datetime.timedelta(days=1)
        assert await learn.snapshot_priors(s, org_id=org, now=t_snap) == 1
        # Corrupt the live state so the rollback has something to restore.
        await s.execute(
            update(_Prior)
            .where(_Prior.org_id == org, _Prior.user_id == user, _Prior.feature_key == "tag:X")
            .values(value=2.0)
        )
        # Rollback to just after the snapshot (no feedback in between).
        res = await learn.rollback_priors(
            s, org_id=org, user_id=user, to=t_snap + datetime.timedelta(hours=1)
        )
        live = await _live(s, org, user)
    assert live["tag:X"] == pytest.approx(v_dec)  # snapshot base preserved
    assert live["tag:X"] != pytest.approx(v0)  # NOT a pure rebuild
    assert res.replayed_events == 0
    assert res.top_change is not None
    assert res.top_change.feature_key == "tag:X"
    assert res.top_change.before == pytest.approx(2.0)
    assert res.top_change.after == pytest.approx(v_dec)


async def test_rollback_replays_feedback_after_the_snapshot_up_to_the_cut() -> None:
    org, user = await _org_user()
    base = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=5)
    async with tenant_session(str(org), str(user)) as s:
        # One accept in the log, snapshot it, then a second accept later.
        _fb(s, org=org, user=user, tag_id="X", action="accept", ts=base)
        await s.flush()
        await learn.rebuild_from_feedback(s, org_id=org, user_id=user)
        v1 = (await _live(s, org, user))["tag:X"]
        t_snap = base + datetime.timedelta(minutes=10)
        await learn.snapshot_priors(s, org_id=org, now=t_snap)
        t2 = t_snap + datetime.timedelta(days=1)
        _fb(s, org=org, user=user, tag_id="X", action="accept", ts=t2)
        await s.flush()
        # Rollback to AFTER the 2nd accept -> base(v1) + replay(1 accept) = v2.
        res = await learn.rollback_priors(
            s, org_id=org, user_id=user, to=t2 + datetime.timedelta(hours=1)
        )
        live = await _live(s, org, user)
    assert res.replayed_events == 1
    assert live["tag:X"] == pytest.approx(learn._saturating_update(v1, 0.08, 1))


async def test_rollback_without_snapshot_degrades_to_truncated_replay() -> None:
    org, user = await _org_user()
    t0 = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=3)
    t1 = t0 + datetime.timedelta(days=1)
    async with tenant_session(str(org), str(user)) as s:
        _fb(s, org=org, user=user, tag_id="X", action="accept", ts=t0)
        _fb(s, org=org, user=user, tag_id="X", action="accept", ts=t1)
        await s.flush()
        # Roll back to between the two accepts: no snapshot -> base {} + the
        # single accept before the cut.
        res = await learn.rollback_priors(
            s, org_id=org, user_id=user, to=t0 + datetime.timedelta(hours=1)
        )
        live = await _live(s, org, user)
    assert res.snapshot_at is None
    assert res.replayed_events == 1
    assert live["tag:X"] == pytest.approx(learn._saturating_update(0.0, 0.08, 1))


# --- telemetry ---------------------------------------------------------


async def test_reject_hotspots_rank_most_declined_first() -> None:
    org, user = await _org_user()
    now = datetime.datetime.now(datetime.UTC)
    async with tenant_session(str(org), str(user)) as s:
        _fb(s, org=org, user=user, tag_id="X", action="reject", ts=now - datetime.timedelta(days=1))
        _fb(s, org=org, user=user, tag_id="X", action="ignore", ts=now - datetime.timedelta(days=2))
        _fb(s, org=org, user=user, tag_id="Y", action="reject", ts=now - datetime.timedelta(days=1))
        # An accept must NOT count as a decline.
        _fb(s, org=org, user=user, tag_id="X", action="accept", ts=now - datetime.timedelta(days=3))
        await s.flush()
        hot = await learn.reject_hotspots(s, org_id=org, user_id=user, now=now)
    assert [(h.feature_key, h.declines) for h in hot] == [("tag:X", 2), ("tag:Y", 1)]


async def test_rollback_coerces_naive_to_and_clamps_future() -> None:
    """A naive `to` is treated as UTC (not silently shifted) and a future
    `to` is clamped to now (a rebuild-to-now, never a forward jump)."""
    org, user = await _org_user()
    now = datetime.datetime.now(datetime.UTC)
    async with tenant_session(str(org), str(user)) as s:
        _fb(s, org=org, user=user, tag_id="X", action="accept", ts=now - datetime.timedelta(days=1))
        await s.flush()
        # Naive + far-future `to`: must not raise (coerced) and must clamp.
        res = await learn.rollback_priors(
            s,
            org_id=org,
            user_id=user,
            to=datetime.datetime(2099, 1, 1, 12, 0),  # naive, future
            now=now,
        )
    assert res.rolled_back_to == now  # coerced aware + clamped to now


async def test_rollback_restores_decay_clock_to_rollback_era_not_now() -> None:
    """A rolled-back base prior keeps a rollback-era updated_at (the decay
    gate), not a reset-to-now, so decay resumes instead of taking a holiday."""
    org, user = await _org_user()
    now = datetime.datetime.now(datetime.UTC)
    t0 = now - datetime.timedelta(days=40)
    t_snap = now - datetime.timedelta(days=39)
    async with tenant_session(str(org), str(user)) as s:
        _fb(s, org=org, user=user, tag_id="X", action="accept", ts=t0)
        await s.flush()
        await learn.rebuild_from_feedback(s, org_id=org, user_id=user)
        await learn.snapshot_priors(s, org_id=org, now=t_snap)  # blob captures X
        # A later reject (AFTER the rollback cut) so the rollback restores base.
        _fb(s, org=org, user=user, tag_id="X", action="reject", ts=now - datetime.timedelta(days=1))
        await s.flush()
        await learn.rollback_priors(
            s, org_id=org, user_id=user, to=t_snap + datetime.timedelta(hours=1), now=now
        )
        row = (
            await s.execute(
                select(_Prior).where(
                    _Prior.org_id == org, _Prior.user_id == user, _Prior.feature_key == "tag:X"
                )
            )
        ).scalar_one()
        ua = row.updated_at
    # updated_at restored to the snapshot era (base_at), NOT stamped at now.
    assert abs((ua - t_snap).total_seconds()) < 5
    assert (now - ua).total_seconds() > 86400  # decisively older than now


async def test_prior_drift_measures_move_vs_old_snapshot_else_empty() -> None:
    org, user = await _org_user()
    now = datetime.datetime.now(datetime.UTC)
    async with tenant_session(str(org), str(user)) as s:
        # No baseline snapshot yet -> nothing to compare against.
        assert await learn.prior_drift(s, org_id=org, user_id=user, now=now) == []
        # A baseline 40 days ago + a current live prior -> a measured move.
        s.add(
            _Snapshot(
                org_id=org,
                user_id=user,
                snapshot_at=now - datetime.timedelta(days=40),
                blob={"tag:X": 0.5},
            )
        )
        s.add(_Prior(org_id=org, user_id=user, feature_key="tag:X", value=1.0, updated_at=now))
        await s.flush()
        drift = await learn.prior_drift(s, org_id=org, user_id=user, days=30, now=now)
    assert len(drift) == 1
    assert drift[0].feature_key == "tag:X"
    assert drift[0].before == pytest.approx(0.5)
    assert drift[0].after == pytest.approx(1.0)
    assert drift[0].delta == pytest.approx(0.5)
