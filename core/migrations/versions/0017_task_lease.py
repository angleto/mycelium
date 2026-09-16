"""Possession of a task, as a row that can expire, so concurrent agents
stop colliding on a column that only ever said where the work was.

The workspace runs fifteen agent sessions at once: ten pull from the
first station, work, and move the task on; five pull from the
verification station and either close it or send it back. Three
failures come out of that, and they are one absence seen from three
angles.

- Two sessions pull the same task. ``set_state`` already takes an
  ``expected_version``, so nothing is lost in the database -- the second
  writer gets a conflict. What is lost is the work it had already done
  before the conflict, and no version gate gives that back. The read and
  the write are two round trips, the ranking that produced the read is
  deterministic, and fifteen callers asking a deterministic ranking the
  same question get the same answer.
- A session moves a task to the verification station and keeps working
  on it. It had nothing to hand back: it moved a column. Possession
  never existed, so surrendering it could not be a step.
- A session dies and its task stays in the working state forever.
  Nothing expires, and no sweep looks: this is the only one of the three
  that is a plain missing backstop.

``in_progress`` had been doing a lock's job without being one. Anyone
may write it, it names no holder, it does not expire, and it cannot be
released because it was never taken.

**Why a lease here when the design that closed on 2026-09-03 refused
one.** That refusal (note 7d101690 §4 B5) is correct and does not reach
this table, by the criterion it states itself: leases belong to actors
whose external effect must survive a mid-flight crash, and the object it
weighed was a write to a note part, which has no such effect -- a stale
writer takes a conflict, a crashed one leaves nothing to recover. An
agent *executing* a task edits a working tree, runs a gate and writes
commits. The effect outlives the crash, and the task row is the only
place recording that the work was in flight. Applied to this object, B5's
own criterion selects for the lease.

Three things B5 refused stay refused, and this table respects them: no
host column on ``agent_tokens``, no presence table (a lease expires by
itself and needs no heartbeat to exist), and ``optimistic_update``
remains the arbiter of concurrent writes to fields -- possession does not
replace it. ADR-0049 is not an obstacle either: it forbids server-side
*session* state and pre-specifies in writing that if multi-agent
coordination lands, it lands as a first-class durable entity with
provenance and RLS. That is this row.

**The index is the whole mechanism.** ``uq_task_leases_live`` is a
partial unique index on ``task_id WHERE released_at IS NULL``: at most
one live possession per task, decided by the datastore rather than by the
code that queries it. Acquiring is an INSERT that wins or loses on that
index, which is what lets a pull be a single round trip instead of the
read-then-write every caller does today. Expiry is deliberately NOT in
the predicate -- an index cannot depend on the clock, so the service
compares ``expires_at`` and the sweep reclaims, exactly as
``device_authorizations`` does for its open-code index.

``fence`` is per task and monotone: a holder that was reclaimed and comes
back carries an old fence and is refused instead of overwriting whoever
holds it now.

Nothing is backfilled. Every task that exists right now is unheld, which
is true: nobody took them, because there was nothing to take.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.create_table(
        "task_leases",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", PG_UUID(as_uuid=True), nullable=False),
        # Four holder slots, because "who" has four answers and they are
        # not interchangeable. ``holder_worker_id`` is the caller's own
        # label for itself and is NOT NULL because it is the only one
        # that discriminates in the configuration actually running here:
        # fifteen sessions share one assistant and one token, so identity
        # and token are constants across all of them and a lease keyed on
        # either would exclude nobody -- every caller would read as the
        # same holder and fifteen acquires would all look idempotent. A
        # worker is not a credential. The other three record what the
        # system already knows about the caller so attribution survives;
        # when the workspace is provisioned with one assistant per agent
        # they all agree, and until then they are the constants above.
        sa.Column("holder_worker_id", sa.String(length=128), nullable=False),
        sa.Column("holder_user_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("holder_identity_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("holder_token_id", PG_UUID(as_uuid=True), nullable=True),
        # The state the possession was taken for. Leaving that state ends
        # the possession, which is the rule that makes "moved it on and
        # kept working" impossible rather than merely discouraged. Stored
        # rather than read back from the task so the reason a lease ended
        # stays legible after the task has moved twice more.
        sa.Column("state_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column(
            "acquired_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        # handoff | done | explicit | expired | preempted. A CHECK rather
        # than a native enum: the set is small, closed and read by humans
        # in a sweep's log, and adding a value to a native enum in
        # PostgreSQL is a migration that cannot run inside a transaction.
        sa.Column("release_reason", sa.Text(), nullable=True),
        sa.Column("fence", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.PrimaryKeyConstraint("id", name="pk_task_leases"),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_task_leases_org_id_organizations",
            ondelete="CASCADE",
        ),
        # A lease cannot outlive its task: it describes a possession OF
        # that row and means nothing without it.
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_task_leases_task_id_tasks",
            ondelete="CASCADE",
        ),
        # The holder can be removed from the workspace; the record that
        # the work was held stays and stops naming them. Same choice as
        # ``note_part.created_by``. No FK on ``holder_token_id``: a token
        # is rotated routinely and the history of who held what must
        # survive the rotation.
        sa.ForeignKeyConstraint(
            ["holder_user_id"],
            ["users.id"],
            name="fk_task_leases_holder_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["holder_identity_id"],
            ["identities.id"],
            name="fk_task_leases_holder_identity_id_identities",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["state_id"],
            ["workflow_states.id"],
            name="fk_task_leases_state_id_workflow_states",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "release_reason IS NULL OR release_reason IN "
            "('handoff', 'done', 'explicit', 'expired', 'preempted')",
            name="ck_task_leases_release_reason",
        ),
        # A released lease has a reason and a reason implies a release.
        # Without this, a sweep that forgot the reason would leave a row
        # that reads as live to the index and as ended to a human.
        sa.CheckConstraint(
            "(released_at IS NULL) = (release_reason IS NULL)",
            name="ck_task_leases_released_together",
        ),
    )
    # The mechanism. See the module docstring: expiry is not in the
    # predicate because an index cannot depend on the clock.
    op.create_index(
        "uq_task_leases_live",
        "task_leases",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.create_index("ix_task_leases_org_id", "task_leases", ["org_id"])
    # "What is this worker holding right now" -- what a session asks when
    # it resumes and does not know what it left behind.
    op.create_index(
        "ix_task_leases_worker_live",
        "task_leases",
        ["holder_worker_id"],
        postgresql_where=sa.text("released_at IS NULL"),
    )
    # The sweep's query: live rows past their deadline, oldest first.
    op.create_index(
        "ix_task_leases_expires_at",
        "task_leases",
        ["expires_at"],
        postgresql_where=sa.text("released_at IS NULL"),
    )
    # "What did this holder last hand off" -- the question the
    # verification station asks before letting somebody pull a task they
    # worked on themselves.
    op.create_index(
        "ix_task_leases_task_released",
        "task_leases",
        ["task_id", "released_at"],
    )

    op.execute("ALTER TABLE task_leases ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE task_leases FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_task_leases ON task_leases USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE task_leases TO mycelium_app")


def downgrade() -> None:
    op.execute("REVOKE ALL ON TABLE task_leases FROM mycelium_app")
    op.execute("DROP POLICY IF EXISTS p_task_leases ON task_leases")
    op.drop_index("ix_task_leases_task_released", table_name="task_leases")
    op.drop_index("ix_task_leases_expires_at", table_name="task_leases")
    op.drop_index("ix_task_leases_worker_live", table_name="task_leases")
    op.drop_index("ix_task_leases_org_id", table_name="task_leases")
    op.drop_index("uq_task_leases_live", table_name="task_leases")
    op.drop_table("task_leases")
