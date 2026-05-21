"""Grant SELECT/INSERT/DELETE on oauth_codes to the application role.

Migration 0061 created the table but forgot the GRANT to ``flow_app``
(the role the backend runs as). Without it the OAuth shim's
``/token`` endpoint hits ``permission denied for table oauth_codes``
on every call. This migration adds the grants idempotently (GRANT
is a no-op when the privilege already exists) so both fresh installs
and already-migrated deployments converge to the right state.

Revision: 0062
Down revision: 0061
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = ("GRANT SELECT, INSERT, DELETE ON oauth_codes TO flow_app",)


DOWNGRADE: tuple[str, ...] = ("REVOKE SELECT, INSERT, DELETE ON oauth_codes FROM flow_app",)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
