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


def test_temporal_facts_are_dated_and_realized(tmp_path: Path) -> None:
    """History-able facts carry ISO effective dates (A3) with
    old < pin < new, and the dates surface in the unit texts so date-pinned
    as-of queries can match them (fresh date in the primary unit, old date in
    the stale unit)."""
    ws = wsgen.generate_workspace(seed=_SEED, scale=_SCALE)
    units = {u.unit_id: u for u in ws.units}
    dated = [f for f in ws.facts if f.valid_from and f.old_valid_from]
    assert dated, "history-able facts must be dated"
    checked = 0
    for f in dated:
        assert f.old_valid_from < f.as_of_pin < f.valid_from  # type: ignore[operator]
        if f.gold_unit_ids and f.value in units[f.gold_unit_ids[0]].text:
            assert f.valid_from in units[f.gold_unit_ids[0]].text
        if f.stale_unit_id:
            assert f.old_valid_from in units[f.stale_unit_id].text
            checked += 1
    assert checked > 0


def test_relational_values_are_registry_persons(tmp_path: Path) -> None:
    """referente/responsabile values are names of PERSON entities in the
    registry (A2), so the multi-hop chain project->lead->attribute resolves
    (a global name pool would never match ``person_by_name``)."""
    ws = wsgen.generate_workspace(seed=_SEED, scale=_SCALE)
    person_names = {e.name for e in ws.entities if e.entity_type == "person"}
    rel = [f for f in ws.facts if f.attribute in ("referente", "responsabile")]
    assert rel, "relational facts must exist"
    assert all(f.value in person_names for f in rel)
    # Competitive fan-out: at least two leads are referente of a project whose
    # target person carries >=2 non-role attributes.
    referente = [f for f in rel if f.attribute == "referente"]
    person_attrs: dict[str, set[str]] = {}
    for f in ws.facts:
        if f.entity_type == "person":
            person_attrs.setdefault(f.entity_name, set()).add(f.attribute)
    resolvable = [f for f in referente if len(person_attrs.get(f.value, set()) - {"ruolo"}) >= 2]
    assert len(resolvable) >= 2


def test_enricher_seam_preserves_or_reverts(tmp_path: Path) -> None:
    """A well-behaved enricher rewrites every unit and records its matrix; a
    lossy one is caught by the fact-preservation verifier and reverted to the
    template text (A11) -- ground truth is never corrupted."""
    good = wsgen.generate_workspace(seed=_SEED, scale=_SCALE, enricher=wsgen.FakeEnricher())
    assert good.enrich_report["reverted_on_loss"] == 0
    assert good.enrich_report["enriched"] == len(good.units)
    assert all(u.generation["provider"] == "fake" for u in good.units)
    # Every dated/queryable value still present after enrichment.
    units = {u.unit_id: u for u in good.units}
    for f in good.facts:
        if f.queryable and f.gold_unit_ids and not f.distributed:
            assert f.value in units[f.gold_unit_ids[0]].text
    lossy = wsgen.generate_workspace(
        seed=_SEED, scale=_SCALE, enricher=wsgen.FakeEnricher(corrupt=True)
    )
    # The verifier catches value loss and reverts (a unit that carries no
    # in-text value has nothing to lose, so not necessarily 100%).
    assert lossy.enrich_report["reverted_on_loss"] > 0
    assert lossy.enrich_report["enriched"] < len(lossy.units)
    assert lossy.enrich_report["reverted_on_loss"] + lossy.enrich_report["enriched"] == len(
        lossy.units
    )


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
