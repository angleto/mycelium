"""Append-only feedback on ``garden_classify`` suggestions (ADR-0037).

Every accept / reject / override / ignore decision the user makes on a
proposal — and every automatic maturity promotion the worker performs
(``action='auto'``) — is one immutable row. It is the materialised
projection the (future) online-learning loop reads from, and the audit
trail that makes ``garden_apply`` and the auto-promotion transparent and
reversible. RLS-scoped by ``org_id`` (ADR-0007).

Note on ``action='auto'``: ADR-0037's table spec lists
``{accept, reject, override, ignore}``; ``auto`` is the distinct
system-initiated promotion (worker running as the workspace owner), kept
separate from a human ``accept`` so telemetry can tell the two apart.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from mycelium_core.models.base import Base, OrgScopedMixin, UUIDPKMixin

# The suggestion kinds and the actions a feedback row can carry. Mirrored
# by the CHECK constraints in migrations 0021 + 0045 (single source: keep
# in sync). ``humus`` (0045, WS-F2) is system-only: the autonomous
# ``humus_flag`` flip in ``distill_note`` records its decision here as
# ``action='auto'``. Like ``cluster`` it is informational for the
# user-facing garden_apply path (``_mutate`` no-ops on it).
SUGGESTION_TYPES: frozenset[str] = frozenset({"tag", "link", "maturity", "cluster", "humus"})
FEEDBACK_ACTIONS: frozenset[str] = frozenset({"accept", "reject", "override", "ignore", "auto"})


class ClassificationFeedback(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "classification_feedback"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The classified node (note in v1). Not an FK: the row is an immutable
    # event that must survive the node's deletion (the learning loop and
    # the audit trail still want the history).
    node_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    suggestion_type: Mapped[str] = mapped_column(String(16), nullable=False)
    suggestion_value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    override_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ts: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    # Copy of the signals that produced the suggestion (transparency +
    # replay). Defaults to an empty object so a bare reject/ignore is legal.
    signals_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
