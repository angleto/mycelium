"""Fase 0 of the search-informed graph (task 561c6aca): the
``RetrievalTraceStage`` write path and its guarantees.

- a normal retrieve appends exactly one ``retrieval_trace`` row whose
  JSONB items mirror the served hits (blob ids in rank order 1..m);
- probe traffic (the eval harness) leaves NO trace while returning
  byte-identical hits (the stage is side-effect only);
- the ``retrieval_trace_enabled`` kill-switch sheds the write;
- RLS: a tenant cannot read another org's traces;
- ``note_edge_usage`` exists and empty is a no-op: the soft-OR weights
  of ``compute_note_edge_weights`` are exactly the link-derived ones
  (the anchor the Phase-2 fourth input must not move when the table
  stays empty).

All against the real DB, mirroring test_coactivity.py.
"""

from __future__ import annotations

import uuid

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import func, select

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.note import NoteKind
from mycelium_core.models.note_edge_usage import NoteEdgeUsage
from mycelium_core.models.retrieval_trace import RetrievalTrace
from mycelium_core.services import eval_offline, note_links
from mycelium_core.services import graph as graph_svc
from mycelium_core.services import memory as mem
from mycelium_core.services import notes as notes_svc
from mycelium_core.services.auth import signup

_FAKE = FakeEmbedder()


async def _org(name: str = "TRC") -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name=name,
        )
    return r.org_id, r.user_id


async def _seed(s, org: uuid.UUID, user: uuid.UUID) -> None:
    for i, text in enumerate(
        ("quokka biology field notes", "quokka habitat report", "unrelated pasta recipe")
    ):
        await mem.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            text_body=text,
            operation_id=f"w{i}",
            embedder=_FAKE,
        )


async def _trace_rows(s, org: uuid.UUID) -> list[RetrievalTrace]:
    return list(
        (
            await s.execute(
                select(RetrievalTrace)
                .where(RetrievalTrace.org_id == org)
                .order_by(RetrievalTrace.created_at)
            )
        )
        .scalars()
        .all()
    )


async def test_retrieve_appends_one_trace_row_mirroring_hits() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed(s, org, user)
        hits = await mem.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            query="quokka",
            operation_id="q-trace",
            embedder=_FAKE,
        )
        assert hits
        rows = await _trace_rows(s, org)
        assert len(rows) == 1
        row = rows[0]
        assert row.is_probe is False
        assert [it["rank"] for it in row.items] == list(range(1, len(hits) + 1))
        assert [it["blob_id"] for it in row.items] == [str(h.blob.id) for h in hits]


async def test_probe_leaves_no_trace_and_hits_are_identical() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed(s, org, user)
        probe_hits = await mem.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            query="quokka",
            operation_id="q-probe",
            embedder=_FAKE,
            probe=True,
        )
        assert not await _trace_rows(s, org)
        live_hits = await mem.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            query="quokka",
            operation_id="q-live",
            embedder=_FAKE,
        )
        # Side-effect only: tracing must not touch the ranking.
        assert [h.blob.id for h in probe_hits] == [h.blob.id for h in live_hits]
        assert len(await _trace_rows(s, org)) == 1


async def test_eval_harness_is_probe_traffic() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed(s, org, user)
        report = await eval_offline.run_eval(
            s,
            org_id=org,
            actor_id=user,
            cases=[eval_offline.GoldCase(query="quokka", expected=frozenset())],
        )
        assert report.n_cases == 1
        assert not await _trace_rows(s, org)


async def test_trace_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    org, user = await _org()
    monkeypatch.setenv("MYCELIUM_RETRIEVAL_TRACE_ENABLED", "false")
    get_settings.cache_clear()
    try:
        async with tenant_session(str(org), str(user)) as s:
            await _seed(s, org, user)
            hits = await mem.retrieve(
                s,
                org_id=org,
                actor_id=user,
                project_id=None,
                query="quokka",
                operation_id="q-off",
                embedder=_FAKE,
            )
            assert hits
            assert not await _trace_rows(s, org)
    finally:
        # monkeypatch restores the env at teardown; the cached singleton
        # must be dropped too or the flipped value leaks to later tests.
        get_settings.cache_clear()


async def test_trace_rls_blocks_cross_org_reads() -> None:
    org_a, user_a = await _org("TRA")
    org_b, user_b = await _org("TRB")
    async with tenant_session(str(org_a), str(user_a)) as s:
        await _seed(s, org_a, user_a)
        await mem.retrieve(
            s,
            org_id=org_a,
            actor_id=user_a,
            project_id=None,
            query="quokka",
            operation_id="q-rls",
            embedder=_FAKE,
        )
        assert len(await _trace_rows(s, org_a)) == 1
    async with tenant_session(str(org_b), str(user_b)) as s:
        # RLS filters the other org's rows even without a WHERE.
        total = (await s.execute(select(func.count()).select_from(RetrievalTrace))).scalar_one()
        assert total == 0


async def test_edge_usage_empty_is_noop_for_edge_weights() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await notes_svc.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="A", text="body A"
        )
        b = await notes_svc.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="B", text="body B"
        )
        await note_links.link_notes(
            s, org_id=org, actor_id=user, parent_note_id=a.id, child_note_id=b.id, kind="related"
        )
        empty = (await s.execute(select(func.count()).select_from(NoteEdgeUsage))).scalar_one()
        assert empty == 0
        weights = await graph_svc.compute_note_edge_weights(s, org_id=org)
        pair = {frozenset((w.src, w.dst)): w.weight for w in weights}
        # The anchor Phase 2 must not move while note_edge_usage stays
        # empty: exactly the link-derived edge, no usage contribution.
        assert frozenset((a.id, b.id)) in pair
        assert len(weights) == 1
