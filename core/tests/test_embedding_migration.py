"""Unit tests for the embedding migration helper + worker behaviour
when the v2 model is unconfigured.

The DB-bound backfill SELECT/UPDATE round-trip is exercised through
the integration suite; here we keep to the no-v2-model short-circuit
and the migration_status math.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from flow_core.embedder import set_embedder_v2_override
from flow_core.services.embedding_migration import (
    migration_status,
    run_embedding_migration,
)


@pytest.fixture(autouse=True)
def _reset_v2_override() -> Iterator[None]:
    yield
    set_embedder_v2_override(None)


async def test_run_embedding_migration_short_circuits_without_v2_model() -> None:
    """If FLOW_EMBED_MODEL_V2 is empty, get_embedder_v2() returns None
    and the migration is a no-op (no SELECT, no UPDATE)."""
    # No override, no env -> v2 disabled.
    session = AsyncMock()
    result = await run_embedding_migration(session, batch_size=10)
    assert result == 0
    # No DB calls made.
    session.execute.assert_not_called()


async def test_migration_status_returns_counts() -> None:
    """migration_status SELECTs two counters and packs into a dict."""
    session = AsyncMock()
    # Two separate scalar_one calls: first returns total, second returns migrated.
    total_result = MagicMock()
    total_result.scalar_one.return_value = 100
    migrated_result = MagicMock()
    migrated_result.scalar_one.return_value = 35
    session.execute = AsyncMock(side_effect=[total_result, migrated_result])

    out = await migration_status(session)
    assert out == {"total": 100, "migrated": 35, "pending": 65}
