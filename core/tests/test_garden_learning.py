"""Online learning loop on garden suggestions (ADR-0037, task 49d24048).

The loop ADR-0032 left open: feedback was write-only. These cover the
closing half -- the saturating update, the floor-preserving read-back in
``classify_node``, the nightly decay/consolidation, and the
event-sourced rebuild (reversibility).
"""

from __future__ import annotations

import datetime
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_core.db import admin_session, tenant_session  # noqa: E402
from mycelium_core.models.classification_feedback import ClassificationFeedback  # noqa: E402
from mycelium_core.models.classification_personal_prior import (  # noqa: E402
    ClassificationPersonalPrior,
)
from mycelium_core.models.note import NoteKind  # noqa: E402
from mycelium_core.models.note_tag import NoteTag  # noqa: E402
from mycelium_core.models.tag import TagKind  # noqa: E402
from mycelium_core.services import garden_classify as classify_svc  # noqa: E402
from mycelium_core.services import garden_learning as learn  # noqa: E402
from mycelium_core.services import notes as notes_svc  # noqa: E402
from mycelium_core.services import taxonomy  # noqa: E402
from mycelium_core.services.auth import signup  # noqa: E402


async def _org_user() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="GL",
        )
    return a.org_id, a.user_id


# --- pure update rule (ADR-0037) ---------------------------------------


def test_saturating_update_grows_with_aligned_feedback_but_never_runs_away() -> None:
    # Repeated accepts climb monotonically with *diminishing* steps and stay
    # under the cap -- the runaway ADR-0033 M3 warns about cannot happen.
    p = 0.0
    last_step = None
    for _ in range(2000):
        nxt = learn._saturating_update(p, 0.08, 1)
        step = nxt - p
        assert step >= 0  # aligned feedback never moves against its sign
        if last_step is not None:
            assert step <= last_step + 1e-9  # steps shrink (saturation), -> 0 at the clamp
        last_step, p = step, nxt
    assert 0 < p <= learn.PRIOR_CAP
    assert p == pytest.approx(learn.PRIOR_CAP, abs=0.05)  # asymptotes at the cap


def test_saturating_update_is_sign_symmetric_and_recovers_fast() -> None:
    assert learn._saturating_update(0.0, 0.08, 1) == pytest.approx(0.04)  # first accept
    assert learn._saturating_update(0.0, 0.08, -1) == pytest.approx(-0.04)  # first reject
    # A single opposing step from deep in the well is LARGE (fast correction).
    deep_neg = -2.0
    recover = learn._saturating_update(deep_neg, 0.08, 1) - deep_neg
    assert recover > 0.07  # ~eta * (1 - sigmoid(-2)) -> close to full eta


def test_prior_factor_is_one_at_neutral_and_bounded() -> None:
    assert learn.prior_factor(0.0) == pytest.approx(1.0)
    assert learn.prior_factor(10.0) == pytest.approx(learn.math.exp(learn.PRIOR_CAP))  # clamps up
    assert learn.prior_factor(-10.0) == pytest.approx(
        learn.math.exp(-learn.PRIOR_CAP)
    )  # clamps down


# --- record_decision: which feedback learns ----------------------------


async def test_record_decision_tag_accept_then_reject_and_user_scoped() -> None:
    org, user = await _org_user()
    tag_id = uuid.uuid4()
    val = {"tag_id": str(tag_id)}
    async with tenant_session(str(org), str(user)) as s:
        await learn.record_decision(
            s,
            org_id=org,
            user_id=user,
            suggestion_type="tag",
            suggestion_value=val,
            action="accept",
        )
        priors = await learn.personal_priors(s, org_id=org, user_id=user, suggestion_type="tag")
        assert priors == {f"tag:{tag_id}": pytest.approx(0.04)}
        # A different user shares nothing (per-user store).
        other = await learn.personal_priors(
            s, org_id=org, user_id=uuid.uuid4(), suggestion_type="tag"
        )
        assert other == {}
        # Reject pushes the same feature negative.
        await learn.record_decision(
            s,
            org_id=org,
            user_id=user,
            suggestion_type="tag",
            suggestion_value=val,
            action="reject",
        )
        after = await learn.personal_priors(s, org_id=org, user_id=user, suggestion_type="tag")
        assert after[f"tag:{tag_id}"] < 0.04


async def test_record_decision_skips_auto_and_non_learnable_types() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        # 'auto' is a system promotion, never a personal signal.
        await learn.record_decision(
            s,
            org_id=org,
            user_id=user,
            suggestion_type="tag",
            suggestion_value={"tag_id": str(uuid.uuid4())},
            action="auto",
        )
        # maturity / cluster carry no re-rankable per-user feature.
        await learn.record_decision(
            s,
            org_id=org,
            user_id=user,
            suggestion_type="maturity",
            suggestion_value={"value": "mature"},
            action="accept",
        )
        rows = (
            (
                await s.execute(
                    select(ClassificationPersonalPrior).where(
                        ClassificationPersonalPrior.org_id == org
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows == []  # nothing learned


async def test_record_decision_override_is_negative_on_suggested_positive_on_chosen() -> None:
    org, user = await _org_user()
    suggested, chosen = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(str(org), str(user)) as s:
        await learn.record_decision(
            s,
            org_id=org,
            user_id=user,
            suggestion_type="tag",
            suggestion_value={"tag_id": str(suggested)},
            action="override",
            override_value={"tag_id": str(chosen)},
        )
        priors = await learn.personal_priors(s, org_id=org, user_id=user, suggestion_type="tag")
    assert priors[f"tag:{suggested}"] < 0  # the system's pick was declined
    assert priors[f"tag:{chosen}"] > 0  # the user's pick was endorsed


# --- read-back: re-ranks, never prunes (the keystone) ------------------


async def _seed_cooccurrence(
    s: AsyncSession, org: uuid.UUID, user: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A node tagged A; 5 notes {A,X}, 3 notes {A,Y}, + filler to clear the
    cold-start ramp. Structural ranking is X (support 5) above Y (support 3).
    Returns (node_id, tag_X, tag_Y)."""

    async def note(title: str) -> uuid.UUID:
        n = await notes_svc.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title=title, text=f"b {title}"
        )
        return n.id

    async def gtag(name: str) -> uuid.UUID:
        t = await taxonomy.create_tag(s, org_id=org, actor_id=user, kind=TagKind.generic, name=name)
        return t.id

    a, x, y = await gtag("A"), await gtag("X"), await gtag("Y")
    node_id = await note("node")
    s.add(NoteTag(org_id=org, note_id=node_id, tag_id=a))
    for i in range(5):
        nid = await note(f"x{i}")
        s.add(NoteTag(org_id=org, note_id=nid, tag_id=a))
        s.add(NoteTag(org_id=org, note_id=nid, tag_id=x))
    for i in range(3):
        nid = await note(f"y{i}")
        s.add(NoteTag(org_id=org, note_id=nid, tag_id=a))
        s.add(NoteTag(org_id=org, note_id=nid, tag_id=y))
    for i in range(12):  # filler so node_count >= COLD_START_NODES (damping = 1.0)
        await note(f"f{i}")
    await s.flush()
    return node_id, x, y


async def test_read_back_reranks_tags_but_never_prunes_a_structural_candidate() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        node_id, x, y = await _seed_cooccurrence(s, org, user)

        # Structural baseline (no user_id): X outranks Y on co-occurrence.
        base = await classify_svc.classify_node(
            s, org_id=org, node_id=node_id, kinds=frozenset({"tags"})
        )
        base_ids = [t.tag_id for t in base.tags]
        assert x in base_ids and y in base_ids
        assert base_ids.index(x) < base_ids.index(y)
        x_structural = next(t.confidence for t in base.tags if t.tag_id == x)
        assert "personal_prior_applied" not in base.signals_used

        # The user has strongly rejected X before (prior planted directly to
        # isolate the read-back from the update rule).
        s.add(
            ClassificationPersonalPrior(
                org_id=org, user_id=user, feature_key=f"tag:{x}", value=-2.0
            )
        )
        await s.flush()

        personal = await classify_svc.classify_node(
            s, org_id=org, node_id=node_id, kinds=frozenset({"tags"}), user_id=user
        )
        ids = [t.tag_id for t in personal.tags]
        # Re-ranked: the disliked-but-structurally-strong X now sits BELOW Y...
        assert ids.index(y) < ids.index(x)
        # ...yet is NEVER pruned -- it cleared the structural floor (the
        # ADR-0037 hard constraint: mute, don't silently drop).
        assert x in ids
        x_personal = next(t.confidence for t in personal.tags if t.tag_id == x)
        assert x_personal < x_structural
        assert "personal_prior_applied" in personal.signals_used


# --- decay / consolidation + rebuild (reversibility) -------------------


async def test_decay_shrinks_stale_priors_and_prunes_neutral_ones() -> None:
    org, user = await _org_user()
    now = datetime.datetime.now(datetime.UTC)
    stale = now - datetime.timedelta(days=learn.DECAY_AFTER_DAYS + 5)
    async with tenant_session(str(org), str(user)) as s:
        # A meaningful-but-stale prior, a fresh one, and a near-zero stale one.
        s.add(
            ClassificationPersonalPrior(
                org_id=org, user_id=user, feature_key="tag:s", value=2.0, updated_at=stale
            )
        )
        s.add(
            ClassificationPersonalPrior(
                org_id=org, user_id=user, feature_key="tag:f", value=2.0, updated_at=now
            )
        )
        s.add(
            ClassificationPersonalPrior(
                org_id=org, user_id=user, feature_key="tag:z", value=0.005, updated_at=stale
            )
        )
        await s.flush()
        decayed, pruned = await learn.decay_priors(s, org_id=org, now=now)
        priors = await learn.personal_priors(s, org_id=org, user_id=user, suggestion_type="tag")
    assert pruned == 1  # the near-zero stale prior is consolidated away
    assert "tag:z" not in priors
    assert priors["tag:s"] == pytest.approx(2.0 * learn.DECAY_FACTOR)  # stale decayed
    assert priors["tag:f"] == pytest.approx(2.0)  # fresh untouched (no feedback gate not met)
    assert decayed >= 1


async def test_rebuild_from_feedback_reconstructs_priors_from_the_log() -> None:
    org, user = await _org_user()
    t1, t2 = uuid.uuid4(), uuid.uuid4()
    base = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)

    def fb(tag: uuid.UUID, action: str, n: int) -> ClassificationFeedback:
        return ClassificationFeedback(
            org_id=org,
            user_id=user,
            node_id=uuid.uuid4(),
            suggestion_type="tag",
            suggestion_value={"tag_id": str(tag)},
            action=action,
            model_version="garden-classify-v1",
            ts=base + datetime.timedelta(seconds=n),
        )

    async with tenant_session(str(org), str(user)) as s:
        # An append-only history: T1 accepted twice then rejected; T2 rejected;
        # one 'auto' and one maturity row that must NOT contribute.
        for row in (
            fb(t1, "accept", 1),
            fb(t1, "accept", 2),
            fb(t1, "reject", 3),
            fb(t2, "reject", 4),
            fb(t1, "auto", 5),
        ):
            s.add(row)
        s.add(
            ClassificationFeedback(
                org_id=org,
                user_id=user,
                node_id=uuid.uuid4(),
                suggestion_type="maturity",
                suggestion_value={"value": "mature"},
                action="accept",
                model_version="garden-classify-v1",
                ts=base + datetime.timedelta(seconds=6),
            )
        )
        await s.flush()

        await learn.rebuild_from_feedback(s, org_id=org, user_id=user)
        priors = await learn.personal_priors(s, org_id=org, user_id=user, suggestion_type="tag")

    # Expected = the same saturating replay, in Python, of the tag rows only.
    expect_t1 = 0.0
    for sign in (1, 1, -1):  # accept, accept, reject (auto skipped)
        expect_t1 = learn._saturating_update(expect_t1, learn._ETA["tag"], sign)
    expect_t2 = learn._saturating_update(0.0, learn._ETA["tag"], -1)
    assert priors[f"tag:{t1}"] == pytest.approx(expect_t1)
    assert priors[f"tag:{t2}"] == pytest.approx(expect_t2)
    assert len(priors) == 2  # maturity + auto contributed nothing


async def test_apply_suggestion_reject_writes_feedback_and_prior() -> None:
    """End-to-end wiring: the user-facing apply path records both the
    append-only feedback row AND folds it into the prior, same transaction."""
    org, user = await _org_user()
    tag_id = uuid.uuid4()
    async with tenant_session(str(org), str(user)) as s:
        await classify_svc.apply_suggestion(
            s,
            org_id=org,
            actor_id=user,
            node_id=uuid.uuid4(),
            suggestion_type="tag",
            suggestion_value={"tag_id": str(tag_id)},
            action="reject",  # reject mutates nothing, only records
        )
        priors = await learn.personal_priors(s, org_id=org, user_id=user, suggestion_type="tag")
        feedback = (
            (
                await s.execute(
                    select(ClassificationFeedback).where(ClassificationFeedback.org_id == org)
                )
            )
            .scalars()
            .all()
        )
    assert len(feedback) == 1 and feedback[0].action == "reject"
    assert priors[f"tag:{tag_id}"] == pytest.approx(-0.04)
