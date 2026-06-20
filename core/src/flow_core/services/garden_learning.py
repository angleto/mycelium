"""Online learning loop on garden suggestions (ADR-0037).

Closes the loop ADR-0032 opened: every accept / reject / override the
user makes on a ``classify_node`` suggestion nudges a per-user, per-feature
*prior* that the classifier reads back to re-rank future suggestions. The
prior is event-sourced (a projection of the append-only
``classification_feedback`` log, which rides the ADR-0036 bus), reversible
(``rebuild_from_feedback`` reconstructs it from that log), and bounded
(saturating update + hard clamp -> no runaway).

Three deliberate scoping choices for v1, each load-bearing:

1. **Which features learn.** Only the suggestion surfaces where a
   per-feature prior actually *re-ranks a candidate list*: ``tag`` (per
   tag id -- the headline "this tag is wrong for me" case) and ``link``
   (per link target -- "stop suggesting I link to this hub"). ``maturity``
   and ``cluster`` carry no re-rankable per-user feature -- maturity's
   auto-promotion is a *structural value* decision that must stay
   structural (a personal prior must never trigger or suppress an
   auto-action, ADR-0037 "never suppress a legitimate candidate"), and a
   cluster id is ephemeral (re-keyed every Leiden run). Their feedback
   still lands in ``classification_feedback`` for telemetry; it just
   creates no prior. ``auto`` (the worker's system promotion) is never a
   personal signal and is skipped entirely.

2. **The update rule.** ADR-0037 writes
   ``prior += eta*(1 - 2*sigmoid(prior))*sign``, but that term is 0 at
   ``prior=0`` and drives the prior *toward* 0 from both sides -- a
   contraction to the origin, not learning, which contradicts the ADR's
   own stated intent ("asymptotes around +/-2.5", runaway prevention). We
   implement the intended saturating-growth form
   ``prior += eta*sign*(1 - sigmoid(sign*prior))``: aligned feedback moves
   the prior in the sign direction with *diminishing* steps (saturates),
   opposing feedback makes a *large* correction. A hard clamp at
   +/-``PRIOR_CAP`` is the explicit ceiling.

3. **Read-back never prunes.** The classifier applies the prior factor
   ``exp(value)`` only as a re-rank/display multiplier; the confidence
   *floor* is always checked on the *structural* confidence, so a
   candidate that clears the floor structurally is shown even under a
   strongly negative prior (the user mutes it explicitly, never silently).
"""

from __future__ import annotations

import datetime
import math
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.models.classification_feedback import ClassificationFeedback
from flow_core.models.classification_personal_prior import ClassificationPersonalPrior as _Prior
from flow_core.models.classification_personal_prior_snapshot import (
    ClassificationPersonalPriorSnapshot as _Snapshot,
)

# Per-type learning rate (ADR-0037 "Update rule"). Types absent here carry
# no re-rankable per-user feature (see module docstring) and never learn.
_ETA: dict[str, float] = {"tag": 0.08, "link": 0.05}

# Log-space prior ceiling: factor exp(+/-2.5) ~= [0.082, 12.2] on the base
# signal (ADR-0037). Saturation keeps the update bounded; this is the wall.
PRIOR_CAP = 2.5

# Nightly metabolism (ADR-0037 "Time decay"): a prior with no feedback for
# DECAY_AFTER_DAYS shrinks by DECAY_FACTOR each sweep (a year untouched ->
# ~0.16 of peak). Consolidation: once it decays under PRUNE_EPSILON it is
# deleted -- a neutral prior (factor ~= 1) is indistinguishable from absent,
# so dropping it keeps the sparse store sparse.
DECAY_AFTER_DAYS = 30
DECAY_FACTOR = 0.995
PRUNE_EPSILON = 0.01

# Snapshots are daily (ADR-0037) but the garden sweep ticks every few
# minutes; ``snapshot_priors`` skips a user whose last checkpoint is newer
# than this, so re-running the sweep does not flood the table. 20h (not
# 24) leaves slack so a slightly-early tick still checkpoints each day.
SNAPSHOT_MIN_INTERVAL = datetime.timedelta(hours=20)


def feature_key(suggestion_type: str, value: dict[str, Any] | None) -> str | None:
    """The stable, type-prefixed key a suggestion re-ranks against, or None
    when this suggestion carries no learnable per-user feature.

    - ``tag``  -> ``tag:<tag_id>``         (per tag: mute/boost a tag)
    - ``link`` -> ``link_target:<note_id>`` (per target: mute a link target)
    """
    if not value:
        return None
    if suggestion_type == "tag":
        tag_id = value.get("tag_id")
        return f"tag:{tag_id}" if tag_id else None
    if suggestion_type == "link":
        target_id = value.get("target_id")
        return f"link_target:{target_id}" if target_id else None
    return None


def _saturating_update(prior: float, eta: float, sign: int) -> float:
    """One ADR-0037 saturating step (Python mirror of the SQL upsert used in
    ``record_decision``; the single source of the maths for replay/tests)."""
    nxt = prior + eta * sign * (1.0 - 1.0 / (1.0 + math.exp(-(sign * prior))))
    return max(-PRIOR_CAP, min(PRIOR_CAP, nxt))


def prior_factor(value: float) -> float:
    """Multiplicative re-rank factor for a prior ``value``: ``exp(clamp)``.
    Neutral (value 0) -> 1.0. Bounded to [exp(-CAP), exp(CAP)]."""
    return math.exp(max(-PRIOR_CAP, min(PRIOR_CAP, value)))


async def _apply_one(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    key: str,
    eta: float,
    sign: int,
    now: datetime.datetime,
) -> None:
    """Atomic saturating upsert of one (user, feature) prior. The new value
    is computed in SQL from the EXISTING row so concurrent decisions on the
    same feature can't lose an update (read-modify-write race)."""
    coef = eta * sign
    # prior + coef*(1 - sigmoid(sign*prior)); sigmoid(x)=1/(1+exp(-x)).
    bounded = func.greatest(
        -PRIOR_CAP,
        func.least(
            PRIOR_CAP,
            _Prior.value + coef * (1.0 - 1.0 / (1.0 + func.exp(-(sign * _Prior.value)))),
        ),
    )
    initial = _saturating_update(0.0, eta, sign)  # the no-conflict (first-feedback) value
    stmt = (
        pg_insert(_Prior)
        .values(org_id=org_id, user_id=user_id, feature_key=key, value=initial, updated_at=now)
        .on_conflict_do_update(
            constraint="pk_classification_personal_prior",
            set_={"value": bounded, "updated_at": now},
        )
    )
    await session.execute(stmt)


async def record_decision(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    suggestion_type: str,
    suggestion_value: dict[str, Any],
    action: str,
    override_value: dict[str, Any] | None = None,
    now: datetime.datetime | None = None,
) -> None:
    """Fold one human decision into the user's priors (ADR-0037 update rule).

    No-op for ``action='auto'`` (a system promotion, not a personal signal)
    and for suggestion types that carry no learnable feature. ``override``
    is two updates: negative on the feature that was suggested, positive on
    the feature the user chose instead. Called inside the same transaction
    as the ``classification_feedback`` write, so prior and log stay coherent.
    """
    if action == "auto":
        return
    eta = _ETA.get(suggestion_type)
    if eta is None:
        return
    now = now or datetime.datetime.now(datetime.UTC)
    if action == "override":
        suggested = feature_key(suggestion_type, suggestion_value)
        chosen = feature_key(suggestion_type, override_value)
        if suggested:
            await _apply_one(
                session, org_id=org_id, user_id=user_id, key=suggested, eta=eta, sign=-1, now=now
            )
        if chosen and chosen != suggested:
            await _apply_one(
                session, org_id=org_id, user_id=user_id, key=chosen, eta=eta, sign=1, now=now
            )
        return
    key = feature_key(suggestion_type, suggestion_value)
    if not key:
        return
    sign = 1 if action == "accept" else -1  # reject / ignore -> negative
    await _apply_one(session, org_id=org_id, user_id=user_id, key=key, eta=eta, sign=sign, now=now)


async def personal_priors(
    session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID, suggestion_type: str
) -> dict[str, float]:
    """``{feature_key: value}`` for one user's priors of one suggestion type.
    A single sparse SELECT; the classifier turns each into ``exp(value)``.
    Empty when the type does not learn or the user has no feedback yet."""
    if suggestion_type not in _ETA:
        return {}
    prefix = "tag:" if suggestion_type == "tag" else "link_target:"
    rows = (
        await session.execute(
            select(_Prior.feature_key, _Prior.value).where(
                _Prior.org_id == org_id,
                _Prior.user_id == user_id,
                _Prior.feature_key.startswith(prefix),
            )
        )
    ).all()
    return {key: float(val) for key, val in rows}


async def decay_priors(
    session: AsyncSession, *, org_id: uuid.UUID, now: datetime.datetime | None = None
) -> tuple[int, int]:
    """Nightly metabolism: geometric decay of stale priors + consolidation.

    Shrinks every prior with no feedback for ``DECAY_AFTER_DAYS`` by
    ``DECAY_FACTOR`` (does NOT bump ``updated_at``, so decay keeps applying
    each sweep -- the gate is "no feedback", not "not yet decayed"), then
    deletes priors that have decayed under ``PRUNE_EPSILON`` (a neutral
    prior is indistinguishable from absent). Returns ``(decayed, pruned)``.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    cutoff = now - datetime.timedelta(days=DECAY_AFTER_DAYS)
    decayed = await session.execute(
        update(_Prior)
        .where(_Prior.org_id == org_id, _Prior.updated_at < cutoff)
        .values(value=_Prior.value * DECAY_FACTOR)
    )
    pruned = await session.execute(
        delete(_Prior).where(_Prior.org_id == org_id, func.abs(_Prior.value) < PRUNE_EPSILON)
    )
    return int(decayed.rowcount or 0), int(pruned.rowcount or 0)  # type: ignore[attr-defined]


def _fold_decision(priors: dict[str, float], fb: ClassificationFeedback) -> None:
    """Replay one feedback row into ``priors`` in place, using the exact
    ``record_decision`` maths. Single source of the replay logic, shared by
    ``rebuild_from_feedback`` and ``rollback_priors``."""
    if fb.action == "auto":
        return
    eta = _ETA.get(fb.suggestion_type)
    if eta is None:
        return
    if fb.action == "override":
        suggested = feature_key(fb.suggestion_type, fb.suggestion_value)
        chosen = feature_key(fb.suggestion_type, fb.override_value)
        if suggested:
            priors[suggested] = _saturating_update(priors.get(suggested, 0.0), eta, -1)
        if chosen and chosen != suggested:
            priors[chosen] = _saturating_update(priors.get(chosen, 0.0), eta, 1)
        return
    key = feature_key(fb.suggestion_type, fb.suggestion_value)
    if not key:
        return
    sign = 1 if fb.action == "accept" else -1
    priors[key] = _saturating_update(priors.get(key, 0.0), eta, sign)


async def _overwrite_priors(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    priors: dict[str, float],
    now: datetime.datetime,
) -> int:
    """Replace a user's live prior rows with ``priors`` (neutral ones
    pruned, since a factor ~= 1 is indistinguishable from absent). Returns
    rows written."""
    await session.execute(delete(_Prior).where(_Prior.org_id == org_id, _Prior.user_id == user_id))
    written = 0
    for key, value in priors.items():
        if abs(value) < PRUNE_EPSILON:
            continue
        session.add(
            _Prior(org_id=org_id, user_id=user_id, feature_key=key, value=value, updated_at=now)
        )
        written += 1
    await session.flush()
    return written


async def _all_priors(
    session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID
) -> dict[str, float]:
    """``{feature_key: value}`` across every suggestion type for one user."""
    rows = (
        await session.execute(
            select(_Prior.feature_key, _Prior.value).where(
                _Prior.org_id == org_id, _Prior.user_id == user_id
            )
        )
    ).all()
    return {key: float(val) for key, val in rows}


async def rebuild_from_feedback(
    session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID
) -> int:
    """Reconstruct a user's priors from the append-only feedback log (the
    ADR-0037 reversibility guarantee). Replays every decision in ts order
    through the same saturating update, then overwrites the live rows.

    Decay is forward-only (time-based, not event-based), so this rebuilds
    the *feedback-driven* component only; ``rollback_priors`` (snapshot +
    replay) is the decay-aware point-in-time variant. Returns rows written.
    """
    rows = (
        (
            await session.execute(
                select(ClassificationFeedback)
                .where(
                    ClassificationFeedback.org_id == org_id,
                    ClassificationFeedback.user_id == user_id,
                )
                .order_by(ClassificationFeedback.ts, ClassificationFeedback.id)
            )
        )
        .scalars()
        .all()
    )
    priors: dict[str, float] = {}
    for fb in rows:
        _fold_decision(priors, fb)
    now = datetime.datetime.now(datetime.UTC)
    return await _overwrite_priors(session, org_id=org_id, user_id=user_id, priors=priors, now=now)


async def snapshot_priors(
    session: AsyncSession, *, org_id: uuid.UUID, now: datetime.datetime | None = None
) -> int:
    """Daily checkpoint (ADR-0037 "Snapshots and rollback"): write one
    ``{feature_key: value}`` blob per user with live priors, so rollback is
    decay-aware point-in-time and drift has a real baseline. Skips users
    with no priors (nothing to checkpoint) and users already checkpointed
    within ``SNAPSHOT_MIN_INTERVAL`` (daily-idempotent under a minutes-
    cadence sweep). Returns snapshots written."""
    now = now or datetime.datetime.now(datetime.UTC)
    rows = (
        await session.execute(
            select(_Prior.user_id, _Prior.feature_key, _Prior.value).where(_Prior.org_id == org_id)
        )
    ).all()
    by_user: dict[uuid.UUID, dict[str, float]] = defaultdict(dict)
    for user_id, key, val in rows:
        by_user[user_id][key] = float(val)
    # Users already checkpointed recently: skip so a fast sweep cadence
    # doesn't flood the table (snapshots are daily).
    fresh_rows = (
        await session.execute(
            select(_Snapshot.user_id).where(
                _Snapshot.org_id == org_id,
                _Snapshot.snapshot_at >= now - SNAPSHOT_MIN_INTERVAL,
            )
        )
    ).all()
    already = {uid for (uid,) in fresh_rows}
    written = 0
    for user_id, blob in by_user.items():
        if user_id in already:
            continue
        session.add(_Snapshot(org_id=org_id, user_id=user_id, snapshot_at=now, blob=blob))
        written += 1
    await session.flush()
    return written


@dataclass(frozen=True)
class FeatureDelta:
    """One feature's value before/after a rollback or over a drift window."""

    feature_key: str
    before: float
    after: float

    @property
    def delta(self) -> float:
        return self.after - self.before


@dataclass(frozen=True)
class RollbackResult:
    """Outcome of a point-in-time rollback (ADR-0037): what window was
    restored and the single largest-moved feature for the one-line diff."""

    rolled_back_to: datetime.datetime
    snapshot_at: datetime.datetime | None
    replayed_events: int
    features_changed: int
    top_change: FeatureDelta | None


async def rollback_priors(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    to: datetime.datetime,
    now: datetime.datetime | None = None,
) -> RollbackResult:
    """Restore a user's priors to their state at ``to`` (ADR-0037 rollback).

    Loads the closest snapshot at-or-before ``to`` (the decay-aware base),
    replays the feedback decisions in ``(snapshot_at, to]`` on top, then
    overwrites the live priors and writes a fresh snapshot at ``now``. With
    no snapshot before ``to`` it degrades to a truncated rebuild-from-log
    (decay-unaware, the best possible without a checkpoint). Returns a diff
    summarising the largest-moved feature."""
    now = now or datetime.datetime.now(datetime.UTC)
    before = await _all_priors(session, org_id=org_id, user_id=user_id)

    snap = (
        await session.execute(
            select(_Snapshot)
            .where(
                _Snapshot.org_id == org_id,
                _Snapshot.user_id == user_id,
                _Snapshot.snapshot_at <= to,
            )
            .order_by(_Snapshot.snapshot_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    base: dict[str, float] = dict(snap.blob) if snap is not None else {}
    base_at = snap.snapshot_at if snap is not None else None

    conds = [
        ClassificationFeedback.org_id == org_id,
        ClassificationFeedback.user_id == user_id,
        ClassificationFeedback.ts <= to,
    ]
    if base_at is not None:
        # Feedback up to base_at is already folded into the snapshot blob;
        # replay only the delta after it (strict > avoids double-applying).
        conds.append(ClassificationFeedback.ts > base_at)
    fb_rows = (
        (
            await session.execute(
                select(ClassificationFeedback)
                .where(*conds)
                .order_by(ClassificationFeedback.ts, ClassificationFeedback.id)
            )
        )
        .scalars()
        .all()
    )
    priors = dict(base)
    for fb in fb_rows:
        _fold_decision(priors, fb)

    await _overwrite_priors(session, org_id=org_id, user_id=user_id, priors=priors, now=now)
    after = {k: v for k, v in priors.items() if abs(v) >= PRUNE_EPSILON}
    # A fresh snapshot pins the rolled-back state as the new checkpoint.
    session.add(_Snapshot(org_id=org_id, user_id=user_id, snapshot_at=now, blob=after))
    await session.flush()

    changed = [
        FeatureDelta(feature_key=k, before=before.get(k, 0.0), after=after.get(k, 0.0))
        for k in set(before) | set(after)
        if abs(after.get(k, 0.0) - before.get(k, 0.0)) >= PRUNE_EPSILON
    ]
    top = max(changed, key=lambda d: abs(d.delta), default=None)
    return RollbackResult(
        rolled_back_to=to,
        snapshot_at=base_at,
        replayed_events=len(fb_rows),
        features_changed=len(changed),
        top_change=top,
    )


@dataclass(frozen=True)
class RejectHotspot:
    """A feature the user keeps declining (ADR-0037 reject-hotspot view).
    ``feature_key`` is type-prefixed (``tag:<id>`` / ``link_target:<id>``);
    label resolution is the caller's (the SPA has the tag/note context)."""

    suggestion_type: str
    feature_key: str
    declines: int
    last_declined_at: datetime.datetime


async def reject_hotspots(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    days: int = 90,
    limit: int = 10,
    now: datetime.datetime | None = None,
) -> list[RejectHotspot]:
    """The suggestions the user declines most (reject / ignore / override),
    grouped by the declined feature, most-declined first. Read-only, the
    user's own history only (ADR-0037 privacy). Surfacing them lets the
    user override at the source. Only learnable features (tag / link) carry
    a stable key; other types are skipped (no per-feature grouping)."""
    now = now or datetime.datetime.now(datetime.UTC)
    since = now - datetime.timedelta(days=days)
    rows = (
        (
            await session.execute(
                select(ClassificationFeedback)
                .where(
                    ClassificationFeedback.org_id == org_id,
                    ClassificationFeedback.user_id == user_id,
                    ClassificationFeedback.action.in_(("reject", "ignore", "override")),
                    ClassificationFeedback.ts >= since,
                )
                .order_by(ClassificationFeedback.ts)
            )
        )
        .scalars()
        .all()
    )
    agg: dict[tuple[str, str], tuple[int, datetime.datetime]] = {}
    for fb in rows:
        # The declined feature is always the *suggested* one (for override,
        # the user picked something else; the suggestion was still declined).
        key = feature_key(fb.suggestion_type, fb.suggestion_value)
        if not key:
            continue
        agg_key = (fb.suggestion_type, key)
        count, last = agg.get(agg_key, (0, fb.ts))
        agg[agg_key] = (count + 1, max(last, fb.ts))
    hotspots = [
        RejectHotspot(suggestion_type=stype, feature_key=key, declines=count, last_declined_at=last)
        for (stype, key), (count, last) in agg.items()
    ]
    hotspots.sort(key=lambda h: (-h.declines, h.feature_key))
    return hotspots[:limit]


async def prior_drift(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    days: int = 30,
    limit: int = 10,
    now: datetime.datetime | None = None,
) -> list[FeatureDelta]:
    """Which priors moved the most over the last ``days`` (ADR-0037 drift
    bar chart). Compares the live priors to the snapshot nearest at-or-
    before the window start. Empty until a snapshot that old exists (no
    reference point = no claim). Largest absolute move first."""
    now = now or datetime.datetime.now(datetime.UTC)
    cutoff = now - datetime.timedelta(days=days)
    baseline = (
        await session.execute(
            select(_Snapshot)
            .where(
                _Snapshot.org_id == org_id,
                _Snapshot.user_id == user_id,
                _Snapshot.snapshot_at <= cutoff,
            )
            .order_by(_Snapshot.snapshot_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if baseline is None:
        return []
    base: dict[str, float] = dict(baseline.blob)
    current = await _all_priors(session, org_id=org_id, user_id=user_id)
    drifts = [
        FeatureDelta(feature_key=k, before=base.get(k, 0.0), after=current.get(k, 0.0))
        for k in set(base) | set(current)
        if abs(current.get(k, 0.0) - base.get(k, 0.0)) >= PRUNE_EPSILON
    ]
    drifts.sort(key=lambda d: (-abs(d.delta), d.feature_key))
    return drifts[:limit]


__all__ = [
    "PRIOR_CAP",
    "FeatureDelta",
    "RejectHotspot",
    "RollbackResult",
    "decay_priors",
    "feature_key",
    "personal_priors",
    "prior_drift",
    "prior_factor",
    "rebuild_from_feedback",
    "record_decision",
    "reject_hotspots",
    "rollback_priors",
    "snapshot_priors",
]
