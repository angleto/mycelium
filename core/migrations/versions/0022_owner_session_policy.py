"""Let the owner, in its own session, see every tenant: no more lifting FORCE.

On managed PostgreSQL the migration role is not a superuser, so FORCE ROW
LEVEL SECURITY binds it and a backfill touches zero rows. The runner used to
fix that by lifting FORCE from every forced table for the duration of the
migration: an ACCESS EXCLUSIVE per table, held until commit, while the old
pods still serve traffic. Release 2.3.50 lifted 89 of them and its first
migrate pod died in DeadlockDetected on ``organizations`` against a
transaction of the app. No lock order avoids that, since the app touches
tables in whatever order it likes.

This gives every table under RLS the policy ``owner_session_all`` (on one
without FORCE it is inert, and covers it if it gets FORCE later): permissive,
granted to the table's owner only, true only when the session is the owner's
too. A migrate Job logs in as the owner, so it sees every tenant without
touching FORCE; a SECURITY DEFINER function or trigger runs as the owner on
behalf of ``mycelium_app``, whose session it is, so it stays bound to the
tenant policies. See ``mycelium_core.migration_rls``.

This is the last release that lifts FORCE: the policies do not exist yet when
it starts, so the runner lifts FORCE from all the forced tables once more and
puts it back. From the next release on it lifts nothing.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from mycelium_core.migration_rls import (
    OWNER_POLICY,
    create_owner_session_policy,
    tables_without_owner_policy,
)

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    for table in tables_without_owner_policy(conn):
        # A same-named policy with another body is not ours to keep: it is
        # what tables_without_owner_policy refused to count.
        op.execute(f'DROP POLICY IF EXISTS {OWNER_POLICY} ON public."{table}"')
        create_owner_session_policy(conn, table)


def downgrade() -> None:
    # Deliberately empty. The runner refuses to commit a table under RLS without
    # the policy, so dropping them here would make this downgrade fail on the
    # very database it is meant for (a non-superuser owner). Left in place they
    # are harmless below 0022: they grant the owner nothing it cannot already
    # do, and the runner of an older release lifts FORCE regardless.
    pass
