"""WS-D2 (b8c60940, ADR-0032 P4): autonomous classify-on-ingest.

The garden sweep stamps not-yet-seen notes with the structural Leiden
community the offline graph snapshot already computed + an
``auto_classified_at`` marker, read-only, behind
``garden_autoclassify_enabled``. The marker is asserted directly (robust to
whether the optional clustering extra is installed: a node with no community
is still marked seen, with ``auto_cluster`` NULL).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from flow_core.config import get_settings
from flow_core.db import admin_session, tenant_session
from flow_core.models.note import Note, NoteKind
from flow_core.services import garden_classify, graph_snapshot
from flow_core.services import notes as nt
from flow_core.services.auth import signup
from flow_worker import garden


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="ACLS")
    return r.org_id, r.user_id


async def _make_notes(org: uuid.UUID, user: uuid.UUID, n: int = 3) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    async with tenant_session(str(org), str(user)) as s:
        for i in range(n):
            note = await nt.create_note(
                s,
                org_id=org,
                actor_id=user,
                kind=NoteKind.text,
                title=f"note {i}",
                text=f"a non-trivial body for note {i}",
            )
            ids.append(note.id)
    return ids


async def test_autoclassify_unprocessed_stamps_and_is_idempotent() -> None:
    """The service marks unprocessed notes with the snapshot community + an
    ``auto_classified_at`` marker, and a re-run is a no-op (the marker filters
    already-seen notes out)."""
    org, user = await _org()
    ids = await _make_notes(org, user, 3)
    async with tenant_session(str(org), str(user)) as s:
        await graph_snapshot.refresh_graph_snapshot(s, org_id=org, force=True)
        snap = await graph_snapshot.get_graph_snapshot(s, org_id=org)
        assert snap is not None
        clusters = snap.clusters or {}

        n1 = await garden_classify.autoclassify_unprocessed(s, org_id=org)
        assert n1 == 3
        notes = (await s.execute(select(Note).where(Note.id.in_(ids)))).scalars().all()
        for note in notes:
            assert note.auto_classified_at is not None  # marked seen
            expected = clusters.get(str(note.id))
            assert note.auto_cluster == (expected if isinstance(expected, int) else None)

        # Idempotent: the marker filters the already-classified notes out.
        n2 = await garden_classify.autoclassify_unprocessed(s, org_id=org)
        assert n2 == 0


async def test_autoclassify_is_bounded_by_limit() -> None:
    """The pass drains over ticks (bounded batch, like the search backfills)."""
    org, user = await _org()
    await _make_notes(org, user, 3)
    async with tenant_session(str(org), str(user)) as s:
        await graph_snapshot.refresh_graph_snapshot(s, org_id=org, force=True)
        assert await garden_classify.autoclassify_unprocessed(s, org_id=org, limit=2) == 2
        assert await garden_classify.autoclassify_unprocessed(s, org_id=org, limit=2) == 1
        assert await garden_classify.autoclassify_unprocessed(s, org_id=org, limit=2) == 0


async def test_garden_sweep_autoclassifies_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the flag on, the autonomous garden sweep classifies new notes
    (the snapshot it refreshes feeds the same-tick autoclassify pass).
    ``_all_workspaces`` is pinned to the test org to keep the sweep isolated."""
    org, user = await _org()
    ids = await _make_notes(org, user, 2)

    async def _only_this_org() -> list[uuid.UUID]:
        return [org]

    monkeypatch.setattr(garden, "_all_workspaces", _only_this_org)
    monkeypatch.setattr(get_settings(), "garden_autoclassify_enabled", True)
    await garden.run_once()

    async with tenant_session(str(org), str(user)) as s:
        notes = (await s.execute(select(Note).where(Note.id.in_(ids)))).scalars().all()
        assert notes and all(note.auto_classified_at is not None for note in notes)


async def test_garden_sweep_skips_autoclassify_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The step is opt-in: with the flag off the sweep leaves notes unmarked."""
    org, user = await _org()
    ids = await _make_notes(org, user, 2)

    async def _only_this_org() -> list[uuid.UUID]:
        return [org]

    monkeypatch.setattr(garden, "_all_workspaces", _only_this_org)
    monkeypatch.setattr(get_settings(), "garden_autoclassify_enabled", False)
    await garden.run_once()

    async with tenant_session(str(org), str(user)) as s:
        notes = (await s.execute(select(Note).where(Note.id.in_(ids)))).scalars().all()
        assert notes and all(note.auto_classified_at is None for note in notes)
