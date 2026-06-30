"""Migration upgrade<->downgrade roundtrip (audit T-1).

The prior session's note claimed a "roundtrip downgrade/upgrade della migration"
green gate; none existed -- the 135 ``downgrade()`` bodies were never exercised.
This guards the newest migration's downgrade so a broken reverse path is caught
in CI rather than discovered during an incident rollback.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _alembic_cfg() -> Config:
    return Config(str(ROOT / "core" / "alembic.ini"))


def test_head_migration_downgrade_upgrade_roundtrip() -> None:
    """``alembic downgrade -1`` then ``upgrade head`` must both succeed and land
    back at head. Runs on the sync (owner) engine, exactly like CI/operator
    rollbacks."""
    sync_url = os.environ.get("MYCELIUM_DATABASE_URL_SYNC")
    if not sync_url:
        pytest.skip("MYCELIUM_DATABASE_URL_SYNC not set")
    engine = sa.create_engine(sync_url, future=True)
    try:
        # Defensive: a downgrade that crosses the 0068 boundary recreates 0067's
        # looser unique index (WHERE invalidated_at IS NULL), which legitimate-
        # under-0068 history (a closed + a re-asserted open fact for one triple)
        # would trip. The current head's -1 (0070 civic_number) does not cross
        # it, but clear the KG tables first so this test stays robust if a future
        # head pushes the -1 boundary back across 0068. TRUNCATE bypasses the
        # per-row delete guard; no test relies on another's KG rows persisting.
        with engine.begin() as conn:
            conn.execute(sa.text("TRUNCATE kg_edge, kg_entity CASCADE"))
        cfg = _alembic_cfg()
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            rev = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
        # Head is the latest revision; the roundtrip must restore it exactly.
        assert rev == "0074"
        # And the 0068 KG objects are still present at head (head integrity).
        with engine.connect() as conn:
            idx = conn.execute(
                sa.text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_kg_edge_current'")
            ).scalar_one()
            trg = conn.execute(
                sa.text("SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_kg_edge_no_delete'")
            ).scalar_one()
        assert "valid_to IS NULL" in idx  # the rescoped open-fact predicate
        assert trg == 1  # the delete guard trigger
    finally:
        engine.dispose()
