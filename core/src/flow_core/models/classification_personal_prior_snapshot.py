"""Daily snapshot of a user's classification priors (ADR-0037 follow-up,
task ea2156df).

The learning loop's reversibility *guarantee* already holds via
``garden_learning.rebuild_from_feedback`` (the prior table is a projection
of the append-only ``classification_feedback`` log). But a pure rebuild is
*forward-only* on time decay: it reconstructs the feedback-driven
component, not the decay that the nightly sweep applied along the way. A
point-in-time rollback that also restores decay needs a real checkpoint of
the live state — this table is it.

One row per ``(org, user)`` per snapshot: ``blob`` is the verbatim
``{feature_key: value}`` map at ``snapshot_at``. The worker writes one
daily; ``POST /garden/learning/rollback`` reads the closest one before the
cut, replays the feedback delta on top, and writes a fresh row.
Append-only by use (never updated), RLS-scoped by ``org_id`` (ADR-0007);
``user_id`` cascades so erasing a user drops their checkpoints.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from flow_core.models.base import Base, OrgScopedMixin, UUIDPKMixin


class ClassificationPersonalPriorSnapshot(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "classification_personal_prior_snapshot"
    __table_args__ = (
        # Rollback reads the latest snapshot <= a cut for one user; drift
        # reads the one nearest ~30d ago. Both are (org, user, snapshot_at)
        # range scans, newest-first.
        Index(
            "ix_cpp_snapshot_org_user_at",
            "org_id",
            "user_id",
            "snapshot_at",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Verbatim {feature_key: value} of the user's priors at snapshot_at.
    blob: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
