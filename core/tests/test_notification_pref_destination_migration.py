"""Migration 0040: preferences stored under the old rule get a proved
destination.

Run against real rows: the statements the migration executes, on prefs
written directly with the destinations the old rule allowed (a stranger's
address, an unverified chat, a webpush target), and on prefs that were
already right, which must not be touched.

Observed failing with the statements removed (``STATEMENTS = ()``,
2026-11-27, Postgres 16): the rewrite test fails, the untouched-rows test
passes, as it should.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session
from mycelium_core.models.notification import NotificationChannelKind
from mycelium_core.services.auth import signup

_ROOT = Path(__file__).resolve().parents[2]
C = NotificationChannelKind


def _migration() -> ModuleType:
    path = next((_ROOT / "core" / "migrations" / "versions").glob("0040_*.py"))
    spec = importlib.util.spec_from_file_location("m0040", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sync(statements: list[tuple[str, dict[str, object]]]) -> list[sa.Row]:
    """Run as the migration role, which the RLS policies do not bind: rows
    written under the old rule have to be planted the way the old rule let
    anyone write them, and the migration itself runs under this role."""
    engine = sa.create_engine(get_settings().database_url_sync)
    try:
        with engine.begin() as conn:
            out: list[sa.Row] = []
            for sql, params in statements:
                res = conn.execute(sa.text(sql), params)
                if res.returns_rows:
                    out.extend(res.fetchall())
            return out
    finally:
        engine.dispose()


def _run_migration() -> None:
    _sync([(statement, {}) for statement in _migration().STATEMENTS])


async def _person() -> tuple[uuid.UUID, uuid.UUID, str]:
    email = f"{uuid.uuid4().hex[:10]}@example.test"
    async with admin_session() as s:
        r = await signup(s, email=email, password="pw-strong-123", org_name="MIG40")
    return r.org_id, r.user_id, email


def _write(org: uuid.UUID, user: uuid.UUID, channel: C, target: str, on: bool) -> None:
    """Store a row as the old rule let any caller store it."""
    _sync(
        [
            (
                "INSERT INTO notification_prefs"
                " (org_id, user_id, channel, enabled, target, version)"
                " VALUES (:o, :u, CAST(:c AS notification_channel), :e, :t, 1)"
                " ON CONFLICT (org_id, user_id, channel)"
                " DO UPDATE SET target = EXCLUDED.target, enabled = EXCLUDED.enabled",
                {"o": org, "u": user, "c": channel.value, "e": on, "t": target},
            )
        ]
    )


def _link(user: uuid.UUID, chat_id: int) -> None:
    _sync(
        [
            (
                "INSERT INTO telegram_links (user_id, chat_id, version) VALUES (:u, :c, 1)",
                {"u": user, "c": chat_id},
            )
        ]
    )


def _read(org: uuid.UUID, user: uuid.UUID) -> dict[str, tuple[str, bool, int]]:
    rows = _sync(
        [
            (
                "SELECT channel::text, target, enabled, version FROM notification_prefs"
                " WHERE org_id = :o AND user_id = :u",
                {"o": org, "u": user},
            )
        ]
    )
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


async def test_stored_destinations_are_rewritten_to_proved_ones() -> None:
    org, linked, linked_email = await _person()
    chat_id = uuid.uuid4().int & 0xFFFFFFFF
    _link(linked, chat_id)
    _write(org, linked, C.email, "attacker@example.test", True)
    _write(org, linked, C.telegram, "999", True)
    _write(org, linked, C.webpush, "leftover", True)

    org2, unlinked, _ = await _person()
    _write(org2, unlinked, C.telegram, "424242", True)

    _run_migration()

    got = _read(org, linked)
    assert got["email"][:2] == (linked_email, True)
    assert got["telegram"][:2] == (str(chat_id), True)
    assert got["webpush"][:2] == ("", True)
    # A chat nobody linked cannot stay a destination, so it is switched off.
    assert (_read(org2, unlinked))["telegram"][:2] == ("", False)


async def test_rows_already_right_are_not_touched() -> None:
    org, user, email = await _person()
    _write(org, user, C.email, email, True)
    before = _read(org, user)

    _run_migration()

    assert _read(org, user) == before
