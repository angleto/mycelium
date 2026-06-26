"""Apply the production function-execute posture (ADR-0015). Idempotent.

PostgreSQL grants EXECUTE on every new function to PUBLIC by default, and the
runtime role ``mycelium_app`` is a member of PUBLIC. Production revokes that
default so the app role can call only the functions a migration explicitly
granted to it; a fresh DB built straight from the migrations keeps the default
and silently masks a missing grant -- a function the app calls directly then
works in dev/CI and 500s in prod (the /advisory/what-now -> ``tasks_event_end``
incident, migration 0059).

This re-asserts the posture and is meant to run right AFTER
``alembic upgrade head`` (the migrate Job chains the two), so it also covers
functions added by later migrations -- something a one-shot migration could
not do.

Single source of truth: it executes ``deploy/local/harden_function_acls.sql``
-- the same file the pytest fixture, CI and ``make db-harden`` run. The backend
image bundles that file at ``/app/deploy/local/``.

Decoupled from app Settings exactly like ``core/migrations/env.py``: it reads
``MYCELIUM_DATABASE_URL_SYNC`` (the owner DSN) directly, so the migrate path
needs no JWT/Fernet.

    python -m mycelium_core.db_harden
"""

from __future__ import annotations

import argparse
import os
import pathlib

from sqlalchemy import create_engine

# Sibling of bootstrap_roles.sql. In the backend image it is copied to
# /app/deploy/local/; both the migrate Job (cwd /app) and a local run (cwd repo
# root) resolve this relative path. MYCELIUM_HARDEN_SQL overrides it.
_DEFAULT_SQL = "deploy/local/harden_function_acls.sql"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sql-path",
        default=os.environ.get("MYCELIUM_HARDEN_SQL", _DEFAULT_SQL),
        help="Path to harden_function_acls.sql (default: %(default)s).",
    )
    args = parser.parse_args()

    sql = pathlib.Path(args.sql_path).read_text()
    engine = create_engine(os.environ["MYCELIUM_DATABASE_URL_SYNC"])
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(sql)
    finally:
        engine.dispose()
    print(f"db_harden: applied {args.sql_path}")


if __name__ == "__main__":
    main()
