"""Migration 0025: the narrow keys are granted where the wide ones were.

The transitional any-of sets (``tasks:state`` | ``workflows:write``,
``tags:assign`` | ``tags:write``) let an assistant granted the wide key
before the split keep advancing and filing tasks. Collapsing them is safe
only if every such assistant holds the narrow key first, which is what
0025 does. Asserted against real assistant rows: the statements the
migration runs, run here on rows that need them.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app
from mycelium_core.config import get_settings

_ROOT = Path(__file__).resolve().parents[2]


def _migration() -> ModuleType:
    path = next((_ROOT / "core" / "migrations" / "versions").glob("0025_*.py"))
    spec = importlib.util.spec_from_file_location("m0025", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _assistant(c: AsyncClient, owner: dict[str, str], scope: list[str]) -> str:
    r = await c.post(
        "/ai-assistants",
        headers=owner,
        json={"label": "A", "handle": f"a{uuid.uuid4().hex[:8]}", "scope": scope},
    )
    assert r.status_code in (200, 201), r.text
    return str(r.json()["assistant"]["id"])


def _scopes(ids: list[str]) -> dict[str, list[str]]:
    engine = sa.create_engine(get_settings().database_url_sync)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT id::text, scope FROM ai_assistants WHERE id::text = ANY(:ids)"),
                {"ids": ids},
            ).all()
    finally:
        engine.dispose()
    return {r[0]: sorted(r[1]) for r in rows}


def _run_upgrade_statements() -> None:
    engine = sa.create_engine(get_settings().database_url_sync)
    try:
        with engine.begin() as conn:
            for stmt in _migration().REGRANT_STATEMENTS:
                conn.execute(sa.text(stmt))
    finally:
        engine.dispose()


async def test_every_wide_grant_gains_its_narrow_key_and_nothing_else() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        su = (
            await c.post(
                "/auth/signup",
                json={
                    "email": f"{uuid.uuid4().hex[:10]}@example.test",
                    "password": "pw-strong-123",
                    "workspace_name": "BRIDGE",
                },
            )
        ).json()
        owner = {
            "Authorization": f"Bearer {su['token']}",
            "X-Workspace-Id": su["workspace_id"],
            "X-Workspace-Role": "owner",
        }
        wide_wf = await _assistant(c, owner, ["tasks:read", "workflows:write"])
        wide_tags = await _assistant(c, owner, ["tags:write"])
        both = await _assistant(c, owner, ["workflows:write", "tags:write"])
        already = await _assistant(c, owner, ["workflows:write", "tasks:state"])
        unrelated = await _assistant(c, owner, ["notes:read"])

    _run_upgrade_statements()
    _run_upgrade_statements()  # idempotent: a second run adds nothing

    got = _scopes([wide_wf, wide_tags, both, already, unrelated])
    assert got[wide_wf] == ["tasks:read", "tasks:state", "workflows:write"]
    assert got[wide_tags] == ["tags:assign", "tags:write"]
    assert got[both] == ["tags:assign", "tags:write", "tasks:state", "workflows:write"]
    assert got[already] == ["tasks:state", "workflows:write"]
    assert got[unrelated] == ["notes:read"]
