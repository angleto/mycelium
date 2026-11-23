"""Link proposals and the built-in proposers (ADR-0073 decision 7)."""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import select, update

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.errors import ConflictError
from mycelium_core.models.link_proposal import LinkProposal
from mycelium_core.models.note import NoteKind
from mycelium_core.services import link_proposals as lp
from mycelium_core.services import link_proposers, note_links
from mycelium_core.services import notes as nt
from mycelium_core.services.auth import signup


@pytest.fixture(autouse=True)
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


@dataclass(frozen=True)
class W:
    org: uuid.UUID
    user: uuid.UUID


async def _ws() -> W:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="PROP",
        )
    return W(r.org_id, r.user_id)


async def _note(w: W, title: str, text: str = "body") -> uuid.UUID:
    async with tenant_session(str(w.org), str(w.user)) as s:
        n = await nt.create_note(
            s, org_id=w.org, actor_id=w.user, kind=NoteKind.text, title=title, text=text
        )
        return n.id


async def _submit(w: W, a: uuid.UUID, b: uuid.UUID, proposer: str = "test:p", score: float = 1.0):  # type: ignore[no-untyped-def]
    async with tenant_session(str(w.org), str(w.user)) as s:
        return await lp.submit(
            s,
            org_id=w.org,
            proposer=proposer,
            source_note_id=a,
            target_note_id=b,
            evidence={"quote": "q"},
            evidence_ref="ev:1",
            score=score,
        )


async def _queue(w: W, note_id: uuid.UUID | None = None) -> list[lp.QueueItem]:
    async with tenant_session(str(w.org), str(w.user)) as s:
        return await lp.queue(s, org_id=w.org, note_id=note_id)


async def _precision(w: W) -> dict[str, lp.ProposerPrecision]:
    async with tenant_session(str(w.org), str(w.user)) as s:
        return {p.proposer: p for p in await lp.precision(s, org_id=w.org)}


async def test_accept_creates_a_typed_link_that_keeps_its_proposer() -> None:
    w = await _ws()
    a, b = await _note(w, "A"), await _note(w, "B")
    first = await _submit(w, a, b, proposer="test:one")
    second = await _submit(w, b, a, proposer="test:two")
    assert first.proposal_id and second.proposal_id
    # Idempotent per (proposer, source, target).
    assert (await _submit(w, a, b, proposer="test:one")).proposal_id == first.proposal_id
    (item, *_rest) = await _queue(w, a)
    assert not hasattr(item, "score")
    async with tenant_session(str(w.org), str(w.user)) as s:
        await lp.record_shown(
            s,
            org_id=w.org,
            actor_id=w.user,
            surface="note_panel",
            proposal_ids=[first.proposal_id],
        )
        link = await lp.accept(
            s, org_id=w.org, actor_id=w.user, proposal_id=first.proposal_id, kind="hypha_of"
        )
        assert (link.parent_note_id, link.child_note_id, link.kind) == (a, b, "hypha_of")
        assert (link.origin_proposer, link.origin_evidence_ref) == ("test:one", "ev:1")
        assert link.created_by is not None
    async with tenant_session(str(w.org), str(w.user)) as s:
        outgoing, _incoming = await note_links.list_note_links(s, org_id=w.org, note_id=a)
        assert [(x.origin_proposer, x.origin_proposal_id) for x in outgoing] == [
            ("test:one", first.proposal_id)
        ]
        with pytest.raises(ConflictError):
            await lp.accept(
                s, org_id=w.org, actor_id=w.user, proposal_id=first.proposal_id, kind="related"
            )
    # One judgement answers both proposers of the pair.
    prec = await _precision(w)
    assert (prec["test:one"].accepted, prec["test:two"].accepted) == (1, 1)
    assert prec["test:one"].shown == 1 and prec["test:two"].shown == 0
    assert await _queue(w) == []
    # Already linked: nothing new is opened.
    assert (await _submit(w, a, b, proposer="test:three")).skipped == "linked"


async def test_a_rejected_pair_is_never_proposed_again_but_can_be_linked_by_hand() -> None:
    w = await _ws()
    a, b = await _note(w, "A"), await _note(w, "B")
    res = await _submit(w, a, b)
    async with tenant_session(str(w.org), str(w.user)) as s:
        await lp.reject(s, org_id=w.org, actor_id=w.user, proposal_id=res.proposal_id)  # type: ignore[arg-type]
    for proposer, src, dst in (("test:p", a, b), ("test:other", b, a)):
        assert (await _submit(w, src, dst, proposer=proposer)).skipped == "rejected"
    async with tenant_session(str(w.org), str(w.user)) as s:
        await note_links.link_notes(
            s, org_id=w.org, actor_id=w.user, parent_note_id=a, child_note_id=b, kind="related"
        )
    prec = await _precision(w)
    assert prec["test:p"].rejected == 1
    assert prec["test:p"].precision == 0.0
    assert prec["test:p"].ci_low == 0.0 and prec["test:p"].ci_high is not None


async def test_a_proposer_holds_a_bounded_number_of_open_proposals_per_note() -> None:
    w = await _ws()
    hub = await _note(w, "Hub")
    others = [await _note(w, f"N{i}") for i in range(lp.OPEN_PER_NOTE + 1)]
    results = [await _submit(w, hub, o) for o in others]
    assert [r.skipped for r in results] == [None] * lp.OPEN_PER_NOTE + ["bound"]
    # Another proposer has its own budget.
    assert (await _submit(w, hub, others[-1], proposer="test:other")).proposal_id


async def test_proposals_touching_the_bin_are_not_offered() -> None:
    w = await _ws()
    a, b = await _note(w, "A"), await _note(w, "B")
    await _submit(w, a, b)
    async with tenant_session(str(w.org), str(w.user)) as s:
        n = await nt.get_note(s, org_id=w.org, note_id=b)
        await nt.soft_delete_note(
            s, org_id=w.org, actor_id=w.user, note_id=b, expected_version=n.version
        )
    assert await _queue(w) == []
    c = await _note(w, "C")
    assert (await _submit(w, a, b)).skipped == "not_effective"
    assert (await _submit(w, a, c)).proposal_id


async def test_undecided_proposals_expire_and_are_recorded_as_such() -> None:
    w = await _ws()
    a, b = await _note(w, "A"), await _note(w, "B")
    res = await _submit(w, a, b)
    old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=lp.TTL_DAYS + 1)
    async with tenant_session(str(w.org), str(w.user)) as s:
        await s.execute(
            update(LinkProposal).where(LinkProposal.id == res.proposal_id).values(created_at=old)
        )
    async with tenant_session(str(w.org), str(w.user)) as s:
        assert await lp.expire(s, org_id=w.org) == 1
    prec = await _precision(w)
    assert (prec["test:p"].expired, prec["test:p"].precision) == (1, None)
    # Expiry is not a rejection: the pair can be proposed again.
    assert (await _submit(w, a, b)).proposal_id


# ---------------------------------------------------------------- built-in proposers


async def test_unlinked_mention_proposes_a_title_written_in_another_note() -> None:
    w = await _ws()
    target = await _note(w, "Mycorrhizal networks", "what the fungus trades")
    citing = await _note(
        w, "Forest", "Yesterday we discussed mycorrhizal networks and their trade."
    )
    await _note(w, "Unrelated", "nothing here")
    async with tenant_session(str(w.org), str(w.user)) as s:
        report = await link_proposers.run_unlinked_mention(s, org_id=w.org)
    assert report.submitted == 1
    (item,) = await _queue(w)
    assert (item.proposer, item.source_note_id, item.target_note_id) == (
        link_proposers.UNLINKED_MENTION,
        citing,
        target,
    )
    assert "mycorrhizal networks" in item.evidence["quote"]


async def test_unlinked_mention_refuses_a_title_too_common_to_be_evidence() -> None:
    w = await _ws()
    await _note(w, "Plan", "the plan itself")
    for i in range(link_proposers.MENTION_MIN_DF_CAP + 1):
        await _note(w, f"Note {i}", f"see the plan for item {i}")
    async with tenant_session(str(w.org), str(w.user)) as s:
        report = await link_proposers.run_unlinked_mention(s, org_id=w.org)
    assert report.submitted == 0
    assert report.skipped.get("too_common") == 1


async def test_unlinked_mention_skips_a_pair_already_joined_in_the_text() -> None:
    w = await _ws()
    target = await _note(w, "Spore print", "how to read one")
    await _note(w, "Lab", f"A spore print, see `{str(target)[:8]}`.")
    async with tenant_session(str(w.org), str(w.user)) as s:
        report = await link_proposers.run_unlinked_mention(s, org_id=w.org)
    assert report.submitted == 0
    assert report.skipped.get("written") == 1


async def test_link_add_puts_its_candidates_on_the_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "graph_written_references_enabled", True)
    w = await _ws()
    target = await _note(w, "Target")
    citing = await _note(w, "Citing", f"see `{str(target)[:8]}`")
    async with tenant_session(str(w.org), str(w.user)) as s:
        report = await link_proposers.run_link_add(s, org_id=w.org)
    assert report.submitted == 1
    async with tenant_session(str(w.org), str(w.user)) as s:
        rows = (
            (await s.execute(select(LinkProposal).where(LinkProposal.org_id == w.org)))
            .scalars()
            .all()
        )
    assert [(r.proposer, {r.source_note_id, r.target_note_id}) for r in rows] == [
        (link_proposers.LINK_ADD, {citing, target})
    ]
    assert rows[0].score is not None
