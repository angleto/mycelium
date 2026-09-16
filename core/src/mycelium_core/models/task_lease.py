"""Possession of a task: who holds it, until when, and how it ended.

State says where a task is. Possession says who is on it right now, and
until when. Mycelium modelled the first and not the second, and the
collisions between concurrent agent sessions are all that absence: two
sessions pull the same task because nothing says it is taken; a session
moves a task on and keeps working because it had nothing to hand back;
a session dies and the task is held forever because nothing expires.

The invariant is the partial unique index ``uq_task_leases_live`` on
``task_id WHERE released_at IS NULL``, not any check in this file: at
most one live lease per task, decided by the datastore. Acquiring is an
INSERT that wins or loses on it, which is what makes ``pull`` a single
round trip rather than the read-then-write that fifteen callers of one
deterministic ranking all lose.

Expiry is not in the index predicate (an index cannot depend on the
clock), so "live" at the index and "live" to a caller are different
questions: :meth:`is_live` answers the second and is the one services
ask. A row past its deadline that the sweep has not reached yet still
occupies the index, which is harmless -- acquiring past an expired lease
goes through the reclaim path, never around it.

Migration 0017 carries the full argument, including why this exists
after a closed design refused a lease table.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class LeaseRelease(enum.StrEnum):
    """Why a possession ended. Four of the five are somebody's decision;
    ``expired`` is the absence of one.

    ``handoff`` is the load-bearing value. A lease is held for the
    duration of ONE workflow state, so moving the task to a different
    non-terminal state ends it -- that is what makes "moved it on and
    carried on working" a refused write rather than a discouraged habit.
    The next station's worker takes its own lease.

    ``done`` is the same event into a terminal state, kept apart because
    a reader asking "what did this holder hand off" must not be answered
    with work that was finished rather than passed on.

    ``preempted`` is the owner taking a task back from a holder. It is
    not a failure, and it is distinguished from ``expired`` so that a
    sweep's numbers stay a measure of sessions that died.
    """

    handoff = "handoff"
    done = "done"
    explicit = "explicit"
    expired = "expired"
    preempted = "preempted"


class TaskLease(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "task_leases"

    task_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )

    # Who holds it. ``holder_worker_id`` is the discriminator, and it is
    # NULLABLE, which is the whole compatibility story: a session that
    # opened a worker (``agent_workers``) is held by that worker, and a
    # caller that never opened one -- the web UI, the CLI, the scheduler
    # -- is held by its user. Making it NOT NULL would oblige every one
    # of those to open a worker, which is writing a contract for clients
    # that do not exist yet.
    #
    # A FK to a server-minted row rather than a string the caller picks,
    # because a name a client invents can collide by accident and nothing
    # would notice: the server would believe one worker held both leases,
    # and a renewal from either would extend the other's possession. The
    # credential cannot do this job either -- one authorization covers
    # every session on a machine, and the transport is stateless on
    # purpose, so nothing in a request distinguishes them.
    #
    # The other three record what the request already carried, so
    # attribution survives without ever being consulted as authority.
    holder_worker_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_workers.id", ondelete="SET NULL"),
        nullable=True,
    )
    holder_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    holder_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("identities.id", ondelete="SET NULL"),
        nullable=True,
    )
    # No FK: tokens rotate routinely and the record of who held what has
    # to survive the rotation.
    holder_token_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)

    # The state the possession was taken for. Leaving it ends the lease.
    state_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("workflow_states.id", ondelete="CASCADE"),
        nullable=False,
    )

    acquired_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    renewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)

    # Monotone per task. A holder that was reclaimed and comes back
    # carries an old fence and is refused, instead of writing over
    # whoever holds the task now.
    fence: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    def is_live(self, now: dt.datetime) -> bool:
        """Live to a CALLER: unreleased and not past its deadline.

        Distinct from live to the index, which knows only about
        ``released_at`` because a unique index cannot read the clock. A
        row that is live here is always live there; the converse is what
        the sweep exists to close.
        """
        return self.released_at is None and self.expires_at > now
