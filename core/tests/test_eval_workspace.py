"""WS-EVAL T1 (task c903ec2c): the synthetic-workspace generator's protocol
obligations, encoded as tests -- determinism, quotas (collisions /
distributed / anaphora), anti-surface self-check, metadata-ablation mode,
and the ingest round-trip through the real service layer."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from _fake_embedder import FakeEmbedder

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.services import eval_workspace as wsgen
from mycelium_core.services import memory as mem
from mycelium_core.services.auth import signup

_SEED = 4242
_SCALE = 120


@pytest.fixture()
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


def test_determinism(tmp_path: Path) -> None:
    """Same seed => byte-identical corpus and registry (the benchmark IS the
    artifact, §1.6); different seed => different corpus."""
    a = wsgen.generate_workspace(seed=_SEED, scale=_SCALE)
    b = wsgen.generate_workspace(seed=_SEED, scale=_SCALE)
    wsgen.write_artifacts(a, tmp_path / "a")
    wsgen.write_artifacts(b, tmp_path / "b")
    assert (tmp_path / "a/corpus.jsonl").read_bytes() == (tmp_path / "b/corpus.jsonl").read_bytes()
    assert (tmp_path / "a/registry.jsonl").read_bytes() == (
        tmp_path / "b/registry.jsonl"
    ).read_bytes()
    c = wsgen.generate_workspace(seed=_SEED + 1, scale=_SCALE)
    wsgen.write_artifacts(c, tmp_path / "c")
    assert (tmp_path / "a/corpus.jsonl").read_bytes() != (tmp_path / "c/corpus.jsonl").read_bytes()


def test_quotas_collisions_distributed_anaphora() -> None:
    ws = wsgen.generate_workspace(seed=_SEED, scale=_SCALE)
    gold = [f for f in ws.facts if f.queryable]
    assert gold, "a workspace must contain queryable gold facts"
    # Every gold fact has >= MIN_COLLISIONS same-attribute facts elsewhere
    # (sibling entity or temporal old value) -- the no-unique-tuple rule §1.3.
    for gf in gold:
        rivals = [
            f
            for f in ws.facts
            if f.fact_id != gf.fact_id
            and f.attribute == gf.attribute
            and (f.entity_id != gf.entity_id or f.category == "collision_temporal")
        ]
        assert len(rivals) >= wsgen.MIN_COLLISIONS_PER_GOLD, (
            f"gold {gf.fact_id} ({gf.entity_type}.{gf.attribute}) has only "
            f"{len(rivals)} collision facts"
        )
    n_distributed = sum(1 for f in gold if f.distributed)
    assert n_distributed == round(len(gold) * wsgen.DISTRIBUTED_FRACTION)
    for f in gold:
        if f.distributed:
            assert len(f.gold_unit_ids) >= 2, "distributed fact must span 2+ units"
    single = [f for f in gold if not f.distributed]
    assert sum(1 for f in single if f.anaphoric) == round(len(single) * wsgen.ANAPHORA_FRACTION)
    # Decoy history (§2): temporal chains also on non-queryable entities.
    assert any(f.category == "collision_temporal" and not f.queryable for f in ws.facts)


def test_distributed_fact_value_not_in_primary_unit() -> None:
    """No single unit carries the full tuple of a distributed fact: the
    primary (attribute-bearing) unit must NOT contain the value; the B-side
    carries the value without naming the entity."""
    ws = wsgen.generate_workspace(seed=_SEED, scale=_SCALE)
    units = {u.unit_id: u for u in ws.units}
    checked = 0
    for f in ws.facts:
        if not (f.queryable and f.distributed and len(f.gold_unit_ids) >= 2):
            continue
        primary, side_b = units[f.gold_unit_ids[0]], units[f.gold_unit_ids[1]]
        assert f.value not in primary.text
        assert f.value in side_b.text
        assert f.entity_name not in side_b.text
        checked += 1
    assert checked > 0


def test_anti_surface_selfcheck_passes() -> None:
    ws = wsgen.generate_workspace(seed=_SEED, scale=_SCALE)
    assert ws.ks_report["ks_length_p"] >= wsgen.KS_MIN_P
    assert ws.ks_report["ks_density_p"] >= wsgen.KS_MIN_P
    assert ws.ks_report["position_tvd"] <= ws.ks_report["position_tvd_allowed"]


def test_blank_content_mode_leaks_no_fact_strings(tmp_path: Path) -> None:
    ws = wsgen.generate_workspace(seed=_SEED, scale=_SCALE)
    manifest = wsgen.write_artifacts(ws, tmp_path / "blank", blank_content=True)
    assert manifest["blank_content"] is True
    corpus = (tmp_path / "blank/corpus.jsonl").read_text(encoding="utf-8")
    # Distinctive value kinds only: a person-valued fact (e.g. referente) can
    # legitimately coincide with the ``actor`` metadata field, which the
    # ablation corpus MUST keep (it is metadata, not content).
    distinctive = {"partita_iva", "telefono", "repository", "endpoint", "scadenza", "rinnovo"}
    for f in ws.facts:
        if f.queryable and f.attribute in distinctive:
            assert f.value not in corpus
    for line in corpus.splitlines():
        assert json.loads(line)["text"] == ""


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"wseval-{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="WSEVAL",
        )
    return r.org_id, r.user_id


async def test_ingest_roundtrip_and_gold_retrievable(_embedder: None) -> None:
    """generate -> ingest through the real services -> a distinctive gold
    fact is retrievable lexically in a LATER session (note_search indexes at
    tenant_session commit)."""
    ws = wsgen.generate_workspace(seed=_SEED, scale=_SCALE)
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        result = await wsgen.ingest_workspace(s, org_id=org, actor_id=user, ws=ws)
    mapping = result.units
    assert len(mapping) == len(ws.units)

    units = {u.unit_id: u for u in ws.units}
    target = next(
        f
        for f in ws.facts
        if f.queryable
        and not f.distributed
        and not f.anaphoric
        and f.attribute in ("partita_iva", "telefono", "repository", "endpoint")
        and f.gold_unit_ids
        and units[f.gold_unit_ids[0]].unit_kind == "note"
    )
    gold_unit = units[target.gold_unit_ids[0]]
    gold_note = uuid.UUID(mapping[target.gold_unit_ids[0]]["note_id"])
    async with tenant_session(str(org), str(user)) as s:
        blobs = await wsgen.resolve_unit_blobs(s, org_id=org, note_id=gold_note)
        assert blobs, "the gold note must be indexed at commit"
        hits = await mem.retrieve(
            s,
            org_id=org,
            actor_id=user,
            # Note blobs are project-scoped: None would search project IS
            # NULL and miss everything (the documented _project_pred trap).
            project_id=result.project_ids[gold_unit.project],
            query=target.value,
            operation_id="wseval-probe",
            embedder=FakeEmbedder(),
            probe=True,
        )
        assert any(target.value in (h.blob.text or "") for h in hits), (
            f"gold value {target.value!r} not retrieved lexically"
        )
