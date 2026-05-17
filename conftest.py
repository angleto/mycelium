"""Test-only defaults and async-engine isolation.

CI/explicit env always wins (setdefault). The app keeps a process-wide
async engine; pytest-asyncio uses one event loop per test, so we
dispose and reset the engine after each test (engine-per-loop in
tests). Production pooling is unchanged.
"""

from __future__ import annotations

import os

os.environ.setdefault("FLOW_JWT_SECRET", "test-only-secret-min-32-bytes-aaaaaaaaaa")
# Valid Fernet key (urlsafe-b64 of 32 zero bytes); test-only.
os.environ.setdefault("FLOW_SECRET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

from collections.abc import AsyncIterator

import pytest

import flow_core.db as _db


@pytest.fixture(autouse=True)
async def _dispose_engine() -> AsyncIterator[None]:
    yield
    engine = _db._engine
    if engine is not None:
        await engine.dispose()
        _db._engine = None
        _db._sessionmaker = None
