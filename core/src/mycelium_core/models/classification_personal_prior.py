"""Per-user learned priors over garden-classify suggestions (ADR-0037).

The online-learning loop's *state*: a sparse per-(user, feature) scalar
that re-ranks the structural classifier's suggestions toward what this
user has accepted before, and away from what they keep declining.

Derived, replayable state -- NOT a second source of truth. Every prior
is a deterministic projection of the append-only ``classification_feedback``
log (which itself rides the ADR-0036 bus): ``garden_learning.record_decision``
applies one saturating update per human decision, in the same transaction
as the feedback row, and ``rebuild_from_feedback`` can reconstruct the whole
table from the log. That is the *reversibility* axis of ADR-0037.

Separation of concerns (ADR-0037 "Personal vs structural priors"): the
**structural** layer (Adamic-Adar / PageRank / Leiden) is global per
workspace and owned by the materialisation worker; this **personal** layer
is per-user and never written by that worker, so the two never conflict.

RLS-scoped by ``org_id`` (ADR-0007). The PK is composite
``(org_id, user_id, feature_key)``: one row per feature a user has ever
given feedback on; absent = neutral (prior 0, factor 1).
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import Float, ForeignKey, PrimaryKeyConstraint, String, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from mycelium_core.models.base import Base, OrgScopedMixin


class ClassificationPersonalPrior(OrgScopedMixin, Base):
    __tablename__ = "classification_personal_prior"
    __table_args__ = (
        PrimaryKeyConstraint(
            "org_id", "user_id", "feature_key", name="pk_classification_personal_prior"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The thing being re-ranked, e.g. ``tag:<uuid>`` or ``link_target:<uuid>``.
    # A stable, type-prefixed key so the same store serves every suggestion
    # surface without collisions (see ``garden_learning.feature_key``).
    feature_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # Log-space preference, clamped to +/-PRIOR_CAP (2.5). 0 = neutral. The
    # classifier multiplies a candidate's structural confidence by exp(value),
    # so value lives on [-2.5, 2.5] -> factor on [0.08, 12.2].
    value: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    # Set on feedback ONLY (never by the decay sweep), so "untouched for N
    # days" -- the decay gate -- means "no feedback for N days", not "not yet
    # decayed". This is why there is no onupdate=now() here.
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
