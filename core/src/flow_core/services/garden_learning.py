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
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.models.classification_feedback import ClassificationFeedback
from flow_core.models.classification_personal_prior import ClassificationPersonalPrior as _Prior

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


async def rebuild_from_feedback(
    session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID
) -> int:
    """Reconstruct a user's priors from the append-only feedback log (the
    ADR-0037 reversibility guarantee). Replays every decision in ts order
    through the same saturating update, then overwrites the live rows.

    Decay is forward-only (time-based, not event-based), so this rebuilds
    the *feedback-driven* component; a point-in-time snapshot+rollback that
    also restores decay is the deferred follow-up. Returns rows written.
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
        if fb.action == "auto":
            continue
        eta = _ETA.get(fb.suggestion_type)
        if eta is None:
            continue
        if fb.action == "override":
            suggested = feature_key(fb.suggestion_type, fb.suggestion_value)
            chosen = feature_key(fb.suggestion_type, fb.override_value)
            if suggested:
                priors[suggested] = _saturating_update(priors.get(suggested, 0.0), eta, -1)
            if chosen and chosen != suggested:
                priors[chosen] = _saturating_update(priors.get(chosen, 0.0), eta, 1)
            continue
        key = feature_key(fb.suggestion_type, fb.suggestion_value)
        if not key:
            continue
        sign = 1 if fb.action == "accept" else -1
        priors[key] = _saturating_update(priors.get(key, 0.0), eta, sign)
    await session.execute(delete(_Prior).where(_Prior.org_id == org_id, _Prior.user_id == user_id))
    now = datetime.datetime.now(datetime.UTC)
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


__all__ = [
    "PRIOR_CAP",
    "decay_priors",
    "feature_key",
    "personal_priors",
    "prior_factor",
    "rebuild_from_feedback",
    "record_decision",
]
