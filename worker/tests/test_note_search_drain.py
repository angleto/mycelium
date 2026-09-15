"""The note-search sweep commits per part, so it never holds a lock.

Task 38f81d98. Measured on 2026-09-15, the day 2.3.36 shipped: the first
tick after the rollout rechunked 171 parts inside ONE transaction and held
it for about thirteen minutes. In the same hour the migration of that same
release lost its lock race twice, and the cause was a worker holding long
transactions on the same table: the migration lifts FORCE ROW LEVEL
SECURITY with an ACCESS EXCLUSIVE per table under a 5 second lock_timeout,
so anything long on ``memory_blobs`` beats it. The sweep was one worker
restart away from being a permanent second source of that.

The property is hard to observe directly -- "a lock was not held" is not a
value you can read back -- so it is measured by its consequence: work that
is committed per part SURVIVES an abort of the sweep, and work that is not
does not. Under the old shape every one of these parts would be gone.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import delete, func, select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.models.note import NoteKind
from mycelium_core.models.note_part_index_pointer import NotePartIndexPointer
from mycelium_core.services import note_parts as np
from mycelium_core.services import notes as nt
from mycelium_core.services.auth import signup
from mycelium_worker import note_search_backfill as sweep


@pytest.fixture
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


def _long_body(marker: str, paragraphs: int = 30) -> str:
    blocks = [f"{marker} apertura del documento."]
    for i in range(paragraphs):
        blocks.append(" ".join(f"riempitivo{(i * 7 + j) % 53}" for j in range(60)))
    return "\n\n".join(blocks)


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="DR",
        )
    return r.org_id, r.user_id


async def _chunk_counts(org: uuid.UUID, user: uuid.UUID, part_ids: list[uuid.UUID]) -> list[int]:
    async with tenant_session(str(org), str(user)) as s:
        out = []
        for pid in part_ids:
            out.append(
                (
                    await s.execute(
                        select(func.count())
                        .select_from(NotePartIndexPointer)
                        .where(NotePartIndexPointer.part_id == pid)
                    )
                ).scalar_one()
            )
        return out


async def test_the_sweep_commits_each_part_so_an_abort_keeps_what_it_did(
    _embedder: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, user = await _org()

    # Three parts that need re-chunking, each collapsed to the legacy
    # one-blob shape the way the back-catalogue actually looks.
    part_ids: list[uuid.UUID] = []
    for i in range(3):
        async with tenant_session(str(org), str(user)) as s:
            note = await nt.create_note(
                s, org_id=org, actor_id=user, kind=NoteKind.text, text=f"intestazione {i}"
            )
            part = await np.create_part(
                s, org_id=org, actor_id=user, note_id=note.id, body=_long_body(f"marca{i}")
            )
            part_ids.append(part.id)
    for pid in part_ids:
        async with tenant_session(str(org), str(user)) as s:
            rows = (
                (
                    await s.execute(
                        select(NotePartIndexPointer)
                        .where(NotePartIndexPointer.part_id == pid)
                        .order_by(NotePartIndexPointer.chunk_index)
                    )
                )
                .scalars()
                .all()
            )
            await s.execute(
                delete(MemoryBlob).where(MemoryBlob.id.in_([r.blob_id for r in rows[1:]]))
            )
    assert await _chunk_counts(org, user, part_ids) == [1, 1, 1]

    # Kill the sweep on its THIRD unit of work, not on its third commit.
    # The distinction is the whole test: patching the checkpoint to raise
    # makes a no-checkpoint implementation fail with "did not raise",
    # which measures that the call exists and not that the work survived.
    # Failing the WORK makes both shapes raise, so the only thing that
    # separates them is what is still there afterwards.
    real_rechunk = sweep.note_search.run_rechunk_backfill
    calls = {"n": 0}

    async def _boom(session: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] >= 3:
            raise RuntimeError("worker killed mid-sweep")
        return await real_rechunk(session, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sweep.note_search, "run_rechunk_backfill", _boom)

    with pytest.raises(RuntimeError):
        async with tenant_session(str(org), str(user), actor_kind="system") as s:
            await sweep._drain(s, org_id=org)

    counts = await _chunk_counts(org, user, part_ids)
    done = [c for c in counts if c > 1]
    assert len(done) == 2, (
        f"the sweep lost work it had already finished: chunk counts {counts}. "
        "Two parts were rechunked before the third failed; under one transaction "
        "for the whole batch the abort takes all of them, which is the same shape "
        "that held a lock on memory_blobs for thirteen minutes."
    )
