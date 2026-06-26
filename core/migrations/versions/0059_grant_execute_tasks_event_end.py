"""Grant EXECUTE on tasks_event_end() to the app role.

``advisory._user_busy`` (the free-window feasibility check behind
``/advisory/what-now``) calls ``public.tasks_event_end(start_at,
duration_minutes)`` directly in its overlap query (it is the single source
of truth for an event's end instant, shared with the
``no_overlap_task_participants`` exclusion constraint). The function has
existed since the baseline, but was only ever invoked INDIRECTLY (inside
that exclusion constraint, which the engine evaluates in the owner's
context); the baseline therefore never granted EXECUTE to ``mycelium_app``.

Once ``_user_busy`` began calling it directly as the app role, production
returned 500s: ``InsufficientPrivilegeError: permission denied for function
tasks_event_end`` -- the prod database has PUBLIC execute revoked on public
functions, so only the explicitly-granted ones are callable. (Dev/test DBs
built straight from the baseline keep the default PUBLIC execute, which is
why the test suite never caught it.) An explicit role grant is the durable
fix: it survives a ``REVOKE ... FROM PUBLIC`` and is a harmless no-op where
PUBLIC still has the privilege. We do NOT revoke PUBLIC here -- this is a
trivial IMMUTABLE helper, not a SECURITY DEFINER boundary, and the
exclusion constraint relies on it being broadly callable.

Revision ID: 0059
Revises: 0058
Create Date: 2026-06-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FN = "public.tasks_event_end(timestamp with time zone, integer)"


def upgrade() -> None:
    op.execute(f"GRANT EXECUTE ON FUNCTION {_FN} TO mycelium_app")


def downgrade() -> None:
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_FN} FROM mycelium_app")
