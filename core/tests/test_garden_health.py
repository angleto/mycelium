"""Garden health sensors (ADR-0035).

v1 computes ``accept_rate_classify`` from ``classification_feedback``;
the other sensors declare their floor but return ``value=None`` + a
reason until their data source lands (never a faked number). Plus the
daily-snapshot persistence the nightly worker tick uses.
"""

from __future__ import annotations

import uuid

from flow_core.db import admin_session, tenant_session
from flow_core.models.classification_feedback import ClassificationFeedback
from flow_core.services import garden_health as health_svc
from flow_core.services.auth import signup


async def _org_user() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="GH",
        )
    return a.org_id, a.user_id


def _fb(org: uuid.UUID, user: uuid.UUID, action: str) -> ClassificationFeedback:
    return ClassificationFeedback(
        org_id=org,
        user_id=user,
        node_id=uuid.uuid4(),
        suggestion_type="tag",
        suggestion_value={"tag_id": str(uuid.uuid4())},
        action=action,
        model_version="garden-classify-v1",
    )


async def test_accept_rate_counts_human_decisions_excludes_auto() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        for _ in range(3):
            s.add(_fb(org, user, "accept"))
        s.add(_fb(org, user, "reject"))
        # ``auto`` is the worker's system promotion: it must NOT count as
        # a human decision in the denominator.
        s.add(_fb(org, user, "auto"))
        await s.flush()
        health = await health_svc.compute_health(s, org_id=org)
    m = health.accept_rate_classify_7d
    assert m.floor == health_svc.ACCEPT_RATE_FLOOR
    assert m.value == 0.75  # 3 accept / (3 accept + 1 reject); auto excluded
    assert m.reason is None


async def test_accept_rate_override_counts_as_accepted() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        s.add(_fb(org, user, "accept"))
        s.add(_fb(org, user, "override"))
        s.add(_fb(org, user, "reject"))
        await s.flush()
        health = await health_svc.compute_health(s, org_id=org)
    # accept + override = 2 accepted of 3 decided.
    assert health.accept_rate_classify_7d.value == round(2 / 3, 4)


async def test_accept_rate_none_when_no_decisions() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        health = await health_svc.compute_health(s, org_id=org)
    m = health.accept_rate_classify_30d
    assert m.value is None
    assert m.reason  # a non-empty explanation, not a faked 0.0
    assert m.floor == health_svc.ACCEPT_RATE_FLOOR


async def test_unimplemented_sensors_declare_reason_not_fake_value() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        health = await health_svc.compute_health(s, org_id=org)
    assert health.fungal_lag.value is None and health.fungal_lag.reason
    assert health.leiden_modularity.value is None and health.leiden_modularity.reason
    assert health.time_to_first_link.value is None
    assert health.tag_entropy_local.value is None
    assert health.tag_entropy_local.floor == health_svc.TAG_ENTROPY_FLOOR


async def test_persist_and_read_snapshot() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        s.add(_fb(org, user, "accept"))
        await s.flush()
        await health_svc.persist_snapshot(s, org_id=org)
        snaps = await health_svc.recent_snapshots(s, org_id=org, days=30)
    assert len(snaps) == 1
    metrics = snaps[0].metrics
    assert "accept_rate_classify_7d" in metrics
    assert metrics["accept_rate_classify_7d"]["value"] == 1.0


async def test_persist_snapshot_idempotent_per_day() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        await health_svc.persist_snapshot(s, org_id=org)
        await health_svc.persist_snapshot(s, org_id=org)  # upsert on (org, day)
        snaps = await health_svc.recent_snapshots(s, org_id=org)
    assert len(snaps) == 1
