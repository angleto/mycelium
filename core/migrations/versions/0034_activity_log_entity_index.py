"""The activity log indexed by the entity it is about.

``ix_activity_log_org_entity_entity_id_ts`` lets the liveness clock of one
note (``note_touch.touched_at_of``) be read with an index probe on that
note's own events instead of a scan of the workspace's history. The notes
list ordered by that clock uses it when the filter holds few notes, when the
order is oldest first, or when the newest-first walk would have to go far
back; ``note_touch`` had named this index as the remedy before it was built.

Built CONCURRENTLY, so it is in a migration of its own and outside any
transaction. ``activity_log`` is append-only, written by almost every
request, and the largest table: a plain ``CREATE INDEX`` holds a lock that
blocks every insert into it for the length of the build, which is an outage
of every request that writes activity. A concurrent build takes a lock that
does not conflict with inserts. The price is that it cannot run inside the
migration's transaction: alembic's autocommit block commits what came before
it (0033 included, with its version), builds, and opens a new transaction for
the version of this one. That is why this is not part of 0033: if the build
fails, 0033 stays applied and a rerun repeats only this step.

A failed or interrupted concurrent build leaves the index behind marked
INVALID: present in the catalog, maintained on every insert, and never used.
``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` would see that leftover and do
nothing, so the rerun would succeed and keep the broken index forever. The
upgrade therefore drops an invalid leftover first (also concurrently), then
builds. ``lock_timeout`` follows the repo's ten seconds: the build waits for
transactions already open on the table, and behind a long one it fails and
is retried rather than waiting indefinitely.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_activity_log_org_entity_entity_id_ts"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '10s'")
        try:
            invalid = (
                op.get_bind()
                .execute(
                    sa.text(
                        "SELECT NOT i.indisvalid FROM pg_index i "
                        "JOIN pg_class c ON c.oid = i.indexrelid "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE c.relname = :name AND n.nspname = current_schema()"
                    ),
                    {"name": _INDEX},
                )
                .scalar()
            )
            if invalid:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
                "ON activity_log (org_id, entity, entity_id, ts)"
            )
        finally:
            op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '10s'")
        try:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
        finally:
            op.execute("RESET lock_timeout")
