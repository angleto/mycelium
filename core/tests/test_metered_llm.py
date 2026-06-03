"""Metering at the LLM seam (task a66ba043).

``MeteredLLM`` charges every ``complete()`` on the per-org basis so no
call site can skip metering: local with no rate card is free, local with
a rate card charges credits_per_*, BYOK charges the byok factor with no
rate card, and the same ``operation_id`` never double-charges. The worker
revision-summary sweep (the verified leak) now debits through the seam.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import select

from flow_core.ai_providers import LLMResult, set_llm_override
from flow_core.db import admin_session, tenant_session
from flow_core.models.billing import CostBasis, UsageRecord
from flow_core.models.note import NoteKind
from flow_core.services import billing
from flow_core.services import notes as notes_svc
from flow_core.services.auth import signup
from flow_core.services.llm_resolver import MeteredLLM
from flow_worker import revisions_summary


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


class _FixedLLM:
    """Provider returning a fixed label with fixed token counts."""

    model_id = "fake-meter-llm"

    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        return LLMResult(text="A short label", tokens_in=3, tokens_out=4, model_id=self.model_id)


async def _usage_count(session: object, operation_id: str) -> int:
    rows = (
        (
            await session.execute(  # type: ignore[attr-defined]
                select(UsageRecord).where(UsageRecord.operation_id == operation_id)
            )
        )
        .scalars()
        .all()
    )
    return len(rows)


async def test_metered_llm_local_without_rate_card_is_free() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="MET")
    org, user = a.org_id, a.user_id
    op_id = f"t:{uuid.uuid4().hex}"
    async with tenant_session(str(org), str(user)) as s:
        llm = MeteredLLM(
            _FixedLLM(),
            session=s,
            org_id=org,
            actor_id=user,
            operation_id=op_id,
            basis=CostBasis.local,
        )
        res = await llm.complete(system=None, messages=[("user", "hi")])
        assert res.text == "A short label"
        # No rate card for the model -> free, no usage row, no error.
        assert await _usage_count(s, op_id) == 0


async def test_metered_llm_local_with_rate_card_charges_and_is_idempotent() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="MET")
    org, user = a.org_id, a.user_id
    op_id = f"t:{uuid.uuid4().hex}"
    async with tenant_session(str(org), str(user)) as s:
        await billing.upsert_rate_card(
            s,
            org_id=org,
            actor_id=user,
            model_id=_FixedLLM.model_id,
            provider="local",
            values={"credits_per_input": Decimal(1), "credits_per_output": Decimal(1)},
        )
        await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(100))
        llm = MeteredLLM(
            _FixedLLM(),
            session=s,
            org_id=org,
            actor_id=user,
            operation_id=op_id,
            basis=CostBasis.local,
        )
        await llm.complete(system=None, messages=[("user", "hi")])
        rec = (
            await s.execute(select(UsageRecord).where(UsageRecord.operation_id == op_id))
        ).scalar_one()
        assert rec.basis is CostBasis.local
        assert rec.credits == Decimal("7.0000")  # 3 in + 4 out at 1/credit
        assert await billing.balance(s, org_id=org) == Decimal("93.0000")
        # Re-run the SAME operation_id: idempotent, no second debit.
        await llm.complete(system=None, messages=[("user", "hi")])
        assert await _usage_count(s, op_id) == 1
        assert await billing.balance(s, org_id=org) == Decimal("93.0000")


async def test_metered_llm_byok_charges_factor_without_rate_card() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="MET")
    org, user = a.org_id, a.user_id
    op_id = f"t:{uuid.uuid4().hex}"
    async with tenant_session(str(org), str(user)) as s:
        await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(1))
        llm = MeteredLLM(
            _FixedLLM(),
            session=s,
            org_id=org,
            actor_id=user,
            operation_id=op_id,
            basis=CostBasis.byok,
        )
        await llm.complete(system=None, messages=[("user", "hi")])
        rec = (
            await s.execute(select(UsageRecord).where(UsageRecord.operation_id == op_id))
        ).scalar_one()
        assert rec.basis is CostBasis.byok
        # Default byok factor 0.0001 x (3 + 4) tokens.
        assert rec.credits == Decimal("0.0007")


async def test_revisions_summary_sweep_meters_through_the_seam() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="MET")
    org, user = a.org_id, a.user_id
    # A freshly created note seals a baseline revision with summary NULL:
    # exactly the pending row the sweep back-fills.
    async with tenant_session(str(org), str(user)) as s:
        await notes_svc.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="Note one", text="body"
        )
        await billing.upsert_rate_card(
            s,
            org_id=org,
            actor_id=user,
            model_id=_FixedLLM.model_id,
            provider="local",
            values={"credits_per_input": Decimal(1), "credits_per_output": Decimal(1)},
        )
        await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(100))

    set_llm_override(_FixedLLM)
    try:
        filled = await revisions_summary._summarize_org(org, user, batch=5)
    finally:
        set_llm_override(None)

    assert filled >= 1
    async with tenant_session(str(org), str(user)) as s:
        usage = (
            (
                await s.execute(
                    select(UsageRecord).where(
                        UsageRecord.op == "llm", UsageRecord.model_id == _FixedLLM.model_id
                    )
                )
            )
            .scalars()
            .all()
        )
        # The sweep debited at the seam (leak closed) instead of calling a
        # bare get_llm() that never charged.
        assert len(usage) >= 1
        assert all(u.basis is CostBasis.local for u in usage)
