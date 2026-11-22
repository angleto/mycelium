"""A workflow state says whether it is a checking station.

``task_leases.assert_may_close`` lets a model close work only after a check
by another worker, and read the evidence for that check from
``tasks.handed_over_by_worker_id``: some other worker had passed the task
into the state it is in now. Any state. A worker that pulled a task from
``queued`` into ``working`` was recorded as having passed it on, and once it
let go, another worker could take the task and close it from ``working``
with nothing ever checked. The handoff the guard wanted was a handoff INTO A
CHECKING STATION, and nothing in the schema said which states those are: the
workflow said it in prose, and the docstring of the guard said so.

``workflow_states.checks_work`` is that attribute, stated by whoever writes
the workflow and never derived from a name at runtime. A state that ends the
work (terminal or ``closes_work``) cannot be one, and the constraint says so:
a check happens before the close, in a state the task then leaves.

Backfill: a state named ``verify`` in a workspace's DEFAULT workflow, when it
neither ends nor closes the work. That is the one place the station is known:
the default workflow of the workspaces in use names it, and its description
says a task enters it to be verified. Nothing else is flagged. Every other
workflow, and the one ``create_default_workflow`` makes (todo, in_progress,
done: no station at all), is born without one, so a model there closes
nothing and a person does. That is the safe reading, the same as 0029's: a
model is asked for a check it could have skipped, never the reverse. A
workspace turns the flag on for its own stations with ``update_workflow``,
which is a person's act.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Exported for the test, which runs this exact statement against real rows.
BACKFILL = (
    "UPDATE workflow_states s SET checks_work = true "
    "FROM workflow_defs d "
    "WHERE d.id = s.workflow_id AND d.is_default "
    "AND s.name = 'verify' AND NOT s.is_terminal AND NOT s.closes_work"
)


def upgrade() -> None:
    # Same lock budget as 0029: adding a column with a constant default is a
    # catalog change, but it queues behind any open reader of the table.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.add_column(
        "workflow_states",
        sa.Column("checks_work", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.execute(BACKFILL)
    # ``op.f``: the name is final. The metadata's naming convention would
    # otherwise prefix it a second time.
    op.create_check_constraint(
        op.f("ck_workflow_states_checks_work_not_ending"),
        "workflow_states",
        "NOT (checks_work AND (is_terminal OR closes_work))",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_workflow_states_checks_work_not_ending"), "workflow_states", type_="check"
    )
    op.drop_column("workflow_states", "checks_work")
