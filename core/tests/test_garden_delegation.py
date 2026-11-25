"""Decisions a person hands over to an assistant (ADR-0076, phase B).

What the delegation promises, each asserted against the services:

- nothing is decided on the person's behalf unless they handed that kind
  over, and a kind that could not be taken back (declining a fact) cannot be
  handed over at all;
- approving a synthesis needs a check with the verdict ``supported`` on the
  version being approved, not on an earlier one;
- every agent decision is recorded, counted, and reversible: after the
  reversal the thing is pending again, as before;
- an agent's decision is not a person's judgement: neither the models'
  accept ratio nor the link proposers' precision counts it.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, update

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.delegated_decision import DelegatedDecision
from mycelium_core.models.identity import Identity
from mycelium_core.models.kg import KgEdge
from mycelium_core.models.link_proposal import LinkProposal
from mycelium_core.models.note import Note, NoteKind
from mycelium_core.models.note_link import NoteNoteLink
from mycelium_core.models.organization import Organization
from mycelium_core.models.proposal_check import CheckVerdict, ClaimStatus
from mycelium_core.services import garden_delegation as deleg
from mycelium_core.services import garden_review as review
from mycelium_core.services import kg
from mycelium_core.services import link_proposals as proposals
from mycelium_core.services import notes as nt
from mycelium_core.services.auth import signup

K = deleg.DecisionKind


async def _org() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="DELEG",
        )
    async with tenant_session(str(r.org_id), str(r.user_id)) as s:
        ident = await s.scalar(
            select(Identity.id).where(Identity.org_id == r.org_id, Identity.user_id == r.user_id)
        )
    assert ident is not None
    # The decider's identity stands in for an assistant's: decide() needs an
    # identity to record, and which kind it is matters to the operation
    # layer (executor_identity), which the API test covers.
    return r.org_id, r.user_id, ident


async def _grant(org: uuid.UUID, user: uuid.UUID, *kinds: deleg.DecisionKind) -> None:
    async with tenant_session(str(org), str(user)) as s:
        current = await s.scalar(select(Organization.settings).where(Organization.id == org))
        await s.execute(
            update(Organization)
            .where(Organization.id == org)
            .values(settings={**(current or {}), deleg.SETTINGS_KEY: [k.value for k in kinds]})
        )


async def _proposed(s: object, org: uuid.UUID, user: uuid.UUID, model: str = "small") -> Note:
    n = await nt.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title="Distillation · x",
        text="body",
    )
    n.review_state = "proposed"
    n.origin_model_id = model
    await s.flush()  # type: ignore[attr-defined]
    return n


def _code(e: pytest.ExceptionInfo[DomainError]) -> tuple[str, str | None]:
    return e.value.code, (e.value.params or {}).get("reason")


async def test_nothing_is_decided_that_was_not_handed_over() -> None:
    org, user, agent = await _org()
    async with tenant_session(str(org), str(user)) as s:
        n = await _proposed(s, org, user)
        for assistant, expect in ((None, "not_an_assistant"), (agent, "not_granted")):
            with pytest.raises(DomainError) as e:
                await deleg.decide(
                    s,
                    org_id=org,
                    actor_id=user,
                    assistant_identity_id=assistant,
                    target_kind=deleg.TargetKind.synthesis,
                    target_id=n.id,
                    verdict=deleg.Verdict.decline,
                    expected_version=n.version,
                )
            assert _code(e) == (MessageCode.GARDEN_DELEGATION_REFUSED, expect)
    await _grant(org, user, *K)
    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(DomainError) as e:
            await deleg.decide(
                s,
                org_id=org,
                actor_id=user,
                assistant_identity_id=agent,
                target_kind=deleg.TargetKind.fact,
                target_id=uuid.uuid4(),
                verdict=deleg.Verdict.decline,
            )
        assert _code(e) == (MessageCode.GARDEN_DELEGATION_REFUSED, "not_delegable")
        with pytest.raises(DomainError):
            deleg.normalize(["synthesis_decline", "approve_everything"])


async def test_a_delegated_decline_is_recorded_reversible_and_not_a_persons_verdict() -> None:
    org, user, agent = await _org()
    await _grant(org, user, K.synthesis_decline)
    async with tenant_session(str(org), str(user)) as s:
        n = await _proposed(s, org, user, model="small-x")
        row = await deleg.decide(
            s,
            org_id=org,
            actor_id=user,
            assistant_identity_id=agent,
            target_kind=deleg.TargetKind.synthesis,
            target_id=n.id,
            verdict=deleg.Verdict.decline,
            expected_version=n.version,
            reason="invents a date",
        )
        assert (await s.get(Note, n.id)).review_state == "declined"  # type: ignore[union-attr]
        assert [d.id for d in await deleg.list_decisions(s, org_id=org)] == [row.id]
        assert await review.accept_ratio_by_model(s, org_id=org) == []

        await deleg.revert(s, org_id=org, actor_id=user, decision_id=row.id)
        assert (await s.get(Note, n.id)).review_state == "proposed"  # type: ignore[union-attr]
        # The undo of an agent's decline is the agent's too: it does not
        # subtract from a person's verdicts on the same model.
        assert await review.accept_ratio_by_model(s, org_id=org) == []
        stats = await deleg.stats(s, org_id=org)
        assert [(x.kind, x.decided, x.reversed) for x in stats] == [("synthesis_decline", 1, 1)]
        again = await deleg.revert(s, org_id=org, actor_id=user, decision_id=row.id)
        assert again.reversed_at is not None


async def test_an_agent_approves_only_what_a_check_supports_on_this_version() -> None:
    org, user, agent = await _org()
    await _grant(org, user, K.synthesis_approve_checked)
    async with tenant_session(str(org), str(user)) as s:
        n = await _proposed(s, org, user)

        async def approve() -> DelegatedDecision:
            fresh = await s.get(Note, n.id)
            return await deleg.decide(
                s,
                org_id=org,
                actor_id=user,
                assistant_identity_id=agent,
                target_kind=deleg.TargetKind.synthesis,
                target_id=n.id,
                verdict=deleg.Verdict.approve,
                expected_version=fresh.version,  # type: ignore[union-attr]
            )

        with pytest.raises(DomainError) as e:
            await approve()
        assert _code(e)[1] == "no_supporting_check"

        async def check(verdict: CheckVerdict, status: ClaimStatus) -> None:
            fresh = await s.get(Note, n.id)
            await review.submit_check(
                s,
                org_id=org,
                actor_id=user,
                note_id=n.id,
                expected_version=fresh.version,  # type: ignore[union-attr]
                verdict=verdict,
                summary="s",
                findings=[review.Finding(claim="c", status=status)],
                checker_model_id="m",
                checker_identity_id=agent,
            )

        await check(CheckVerdict.issues, ClaimStatus.unsupported)
        with pytest.raises(DomainError):
            await approve()
        await check(CheckVerdict.supported, ClaimStatus.supported)
        # The note moves after the check: the check no longer speaks for it.
        fresh = await s.get(Note, n.id)
        fresh.version += 1  # type: ignore[union-attr]
        await s.flush()
        with pytest.raises(DomainError) as e:
            await approve()
        assert _code(e)[1] == "no_supporting_check"
        await check(CheckVerdict.supported, ClaimStatus.supported)
        row = await approve()
        assert (await s.get(Note, n.id)).review_state == "approved"  # type: ignore[union-attr]
        assert await review.accept_ratio_by_model(s, org_id=org) == []

        await deleg.revert(s, org_id=org, actor_id=user, decision_id=row.id)
        assert (await s.get(Note, n.id)).review_state == "proposed"  # type: ignore[union-attr]


async def test_a_delegated_link_decision_counts_for_no_proposer_and_is_undone() -> None:
    org, user, agent = await _org()
    await _grant(org, user, K.link_proposal_accept, K.link_proposal_reject)
    async with tenant_session(str(org), str(user)) as s:
        a = await nt.create_note(s, org_id=org, actor_id=user, kind=NoteKind.text, text="a")
        b = await nt.create_note(s, org_id=org, actor_id=user, kind=NoteKind.text, text="b")
        c = await nt.create_note(s, org_id=org, actor_id=user, kind=NoteKind.text, text="c")
        p1 = await proposals.submit(
            s, org_id=org, proposer="p", source_note_id=a.id, target_note_id=b.id
        )
        p2 = await proposals.submit(
            s, org_id=org, proposer="p", source_note_id=a.id, target_note_id=c.id
        )
        assert p1.proposal_id and p2.proposal_id
        acc = await deleg.decide(
            s,
            org_id=org,
            actor_id=user,
            assistant_identity_id=agent,
            target_kind=deleg.TargetKind.link_proposal,
            target_id=p1.proposal_id,
            verdict=deleg.Verdict.approve,
            link_kind="related",
        )
        rej = await deleg.decide(
            s,
            org_id=org,
            actor_id=user,
            assistant_identity_id=agent,
            target_kind=deleg.TargetKind.link_proposal,
            target_id=p2.proposal_id,
            verdict=deleg.Verdict.decline,
        )
        assert acc.result_id is not None
        assert [(x.accepted, x.rejected) for x in await proposals.precision(s, org_id=org)] in (
            [],
            [(0, 0)],
        )

        await deleg.revert(s, org_id=org, actor_id=user, decision_id=acc.id)
        await deleg.revert(s, org_id=org, actor_id=user, decision_id=rej.id)
        assert await s.get(NoteNoteLink, acc.result_id) is None
        statuses = (
            (
                await s.execute(
                    select(LinkProposal.status).where(
                        LinkProposal.id.in_([p1.proposal_id, p2.proposal_id])
                    )
                )
            )
            .scalars()
            .all()
        )
        assert sorted(statuses) == ["proposed", "proposed"]
        # The rejected pair is no longer remembered: it can be proposed again.
        again = await proposals.submit(
            s, org_id=org, proposer="q", source_note_id=a.id, target_note_id=c.id
        )
        assert again.skipped is None


async def test_a_delegated_fact_approval_is_undone_to_proposed() -> None:
    org, user, agent = await _org()
    await _grant(org, user, K.fact_approve)
    async with tenant_session(str(org), str(user)) as s:
        x = await kg.ensure_entity(s, org_id=org, name="X", entity_type="person")
        y = await kg.ensure_entity(s, org_id=org, name="Y", entity_type="organization")
        edge = await kg.add_fact(
            s,
            org_id=org,
            subject_id=x.id,
            predicate="member_of",
            object_id=y.id,
            review_state="proposed",
        )
        row = await deleg.decide(
            s,
            org_id=org,
            actor_id=user,
            assistant_identity_id=agent,
            target_kind=deleg.TargetKind.fact,
            target_id=edge.id,
            verdict=deleg.Verdict.approve,
            expected_version=edge.version,
        )
        assert (await s.get(KgEdge, edge.id)).review_state == "approved"  # type: ignore[union-attr]
        await deleg.revert(s, org_id=org, actor_id=user, decision_id=row.id)
        assert (await s.get(KgEdge, edge.id)).review_state == "proposed"  # type: ignore[union-attr]
