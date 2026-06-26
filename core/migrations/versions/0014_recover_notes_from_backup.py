"""Recover note bodies from the 2026-05-27 Scaleway backup export
for the 10 notes that 0013 could not reach (no ``entity_revision``
snapshot existed, e.g. notes created before revision logging or
never edited after creation).

The backup payload is **not** in the repo: it contains personal note
bodies. It is mounted into the migrate-job at
``/var/lib/flow/recovery/notes_backup.json`` from a ConfigMap created
ad-hoc from the local pg_dump export. The path can be overridden via
``MYCELIUM_NOTES_RECOVERY_PATH``. When the file is absent the migration
is a no-op (which is the case for any environment except the one we
patched, and for re-runs after the ConfigMap is torn down).

Apply path: NO FORCE / FORCE RLS bracket (same trick as the patched
0011 and as 0013), INSERT guarded by ``NOT EXISTS`` so the rows
already restored from ``entity_revision`` in 0013 stay untouched.
Strictly additive: never overwrites a part(ord=0) that already
exists.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from pathlib import Path

from alembic import op
from sqlalchemy import bindparam
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import text

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_PATH = "/var/lib/flow/recovery/notes_backup.json"
_log = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    path = Path(os.environ.get("MYCELIUM_NOTES_RECOVERY_PATH", _DEFAULT_PATH))
    if not path.exists():
        _log.info("0014: recovery payload %s not present, skipping", path)
        return

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not payload:
        _log.info("0014: recovery payload is empty, skipping")
        return

    op.execute("ALTER TABLE notes NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE note_part NO FORCE ROW LEVEL SECURITY")
    try:
        stmt = text(
            """
            WITH backup AS (
                SELECT (rec->>'note_id')::uuid AS note_id,
                       (rec->>'org_id')::uuid  AS org_id,
                       rec->>'body'            AS body
                  FROM jsonb_array_elements(:payload) AS rec
            )
            INSERT INTO note_part (org_id, note_id, ord, body)
            SELECT n.org_id, n.id, 0, b.body
              FROM backup b
              JOIN notes n
                ON n.id = b.note_id
               AND n.org_id = b.org_id
             WHERE n.deleted_at IS NULL
               AND b.body IS NOT NULL
               AND b.body <> ''
               AND NOT EXISTS (
                 SELECT 1 FROM note_part np
                  WHERE np.note_id = n.id AND np.ord = 0
               )
            """
        ).bindparams(bindparam("payload", type_=JSONB))
        result = op.get_bind().execute(stmt, {"payload": payload})
        _log.info("0014: inserted %s note_part(ord=0) rows from backup", result.rowcount)
    finally:
        op.execute("ALTER TABLE note_part FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE notes FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    # No-op: the rows we insert are indistinguishable from regular
    # user-written parts. Removing them would be destructive without
    # a way to identify them.
    pass
