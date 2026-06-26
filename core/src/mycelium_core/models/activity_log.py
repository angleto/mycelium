"""Append-only activity log (audit).

No update/delete from code. Append-only is hardened at the DB level by
a policy/trigger in the baseline migration (docs/adr/0002).
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import Base, OrgScopedMixin, UUIDPKMixin


class ActivityLog(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "activity_log"

    actor_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    # The kind of caller that produced this entry. One of
    # human_direct (SPA), human_api (REST/JWT outside the SPA),
    # human_telegram (conversational bot), agent_run (an LLM agent
    # loop driven by services/agent_runtime), mcp_token (an MCP call
    # authenticated via a long-lived agent token), system (migrations,
    # workers, schedulers). Propagated via the GUC
    # ``app.current_actor_kind`` which is set in db.tenant_session /
    # db.admin_session. CHECK constraint at the DB level keeps the set
    # closed; native enum was avoided so future kinds (e.g. ``adjudication``)
    # are a single ALTER.
    actor_kind: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default="human_direct"
    )
    # When the actor is a non-human subject (agent_run, mcp_token),
    # the ``id`` of that subject (agent_run.id, agent_tokens.id). NULL
    # for human_* and system; their identity is fully in ``actor_id``.
    actor_subject_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    entity: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    diff: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ts: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
