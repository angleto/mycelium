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
from alembic.script import ScriptDirectory

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
        # The head revision, read from the migration scripts -- NOT hard-coded,
        # so this guard follows every new migration instead of failing CI on the
        # commit that adds one (which is exactly when the downgrade path most
        # needs exercising).
        head = ScriptDirectory.from_config(cfg).get_current_head()
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            rev = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
        # The roundtrip must restore the head exactly.
        assert rev == head
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


def test_reference_data_seeds_survive_the_chain() -> None:
    """The seeds a squash silently drops.

    A squash keeps the schema and drops the data. Two revisions of the
    old chain carried SEEDS rather than transformations -- 0074's
    ``system_settings`` singleton and 0043's seven fleet rate cards --
    and the 2026-08-22 squash classified both as schema-only, so a
    database built from the new baseline started life without them.

    The singleton turned CI red (concurrent readers raced to create it);
    the rate cards were worse and silent, because the suite seeds its own
    and nothing asserted the fleet defaults exist -- ``billing`` would
    have raised ``rate_card.not_found`` for every non-BYOK call on a new
    deployment. Migration 0003 restores both.

    This asserts the OUTCOME rather than the migration, so it keeps
    holding if the chain is squashed again: the next squash must carry
    these rows forward or turn this red.
    """
    sync_url = os.environ.get("MYCELIUM_DATABASE_URL_SYNC")
    if not sync_url:
        pytest.skip("MYCELIUM_DATABASE_URL_SYNC not set")
    engine = sa.create_engine(sync_url, future=True)
    try:
        with engine.connect() as conn:
            settings = conn.execute(sa.text("SELECT count(*) FROM system_settings")).scalar_one()
            cards = conn.execute(
                sa.text("SELECT count(*) FROM default_rate_card WHERE is_active")
            ).scalar_one()
            fallbacks = set(
                conn.execute(sa.text("SELECT model_id FROM default_rate_card")).scalars()
            )
        assert settings == 1, "the SdI environment singleton must exist, exactly once"
        assert cards >= 7, "the fleet fallback rate cards must be seeded"
        # The two the resolver falls back to by name when an org has no
        # card of its own; named explicitly so a partial seed is caught.
        assert {"gpt-4o-mini", "claude-3-5-haiku-latest"} <= fallbacks
    finally:
        engine.dispose()
