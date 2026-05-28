"""Decomposition pipeline (task 4a718dc4).

Asserts the Phase 1 invariants:
- a non-trivial note can be distilled exactly once;
- the distillation note carries ``humus_kind='distillation'`` and
  ``humus_flag=True``;
- a NoteNoteLink (``atom_of``) joins the source to the distillation
  so the graph can navigate the lineage;
- re-running on an already-distilled source is a no-op (returns the
  cached distillation id).
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _fake_ai import FakeLLM  # noqa: E402
from sqlalchemy import select  # noqa: E402

from flow_core.ai_providers import set_llm_override  # noqa: E402
from flow_core.db import admin_session, tenant_session  # noqa: E402
from flow_core.models.note import Note, NoteKind  # noqa: E402
from flow_core.models.note_link import NoteNoteLink  # noqa: E402
from flow_core.services import billing  # noqa: E402
from flow_core.services import decomposition as decomp  # noqa: E402
from flow_core.services import notes as nt  # noqa: E402
from flow_core.services.auth import signup  # noqa: E402


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="DECOMP")
    return r.org_id, r.user_id


@pytest.fixture
def _wire_llm() -> Iterator[None]:
    set_llm_override(FakeLLM)
    try:
        yield
    finally:
        set_llm_override(None)


async def _seed_billing(s: object, org: uuid.UUID, user: uuid.UUID) -> None:
    await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(1000))  # type: ignore[arg-type]
    await billing.upsert_rate_card(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        model_id="fake-llm",
        provider="local",
        values={
            "credits_per_input": Decimal("0.001"),
            "credits_per_output": Decimal("0.001"),
        },
    )


async def test_distill_note_creates_humus_atom(_wire_llm: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        source = await nt.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            title="Long thought",
            text=(
                "Decided to stop building Y because the failure mode only "
                "shows up after weeks; the week-2 workaround masked a config "
                "drift between staging and prod."
            ),
        )
        res = await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        distilled = (
            await s.execute(select(Note).where(Note.id == res.distilled_note_id))
        ).scalar_one()
        assert distilled.humus_kind == "distillation"
        assert distilled.humus_flag is True
        await s.refresh(source)
        assert source.humus_flag is True
        link = (
            await s.execute(
                select(NoteNoteLink).where(
                    NoteNoteLink.parent_note_id == source.id,
                    NoteNoteLink.child_note_id == distilled.id,
                )
            )
        ).scalar_one()
        assert link.kind == "atom_of"


async def test_distill_note_is_idempotent(_wire_llm: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        source = await nt.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            title="t",
            text="some non-trivial body the LLM will distill on the first pass",
        )
        r1 = await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        r2 = await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        assert r1.distilled_note_id == r2.distilled_note_id
        rows = (
            (
                await s.execute(
                    select(NoteNoteLink).where(
                        NoteNoteLink.parent_note_id == source.id,
                        NoteNoteLink.kind == "atom_of",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
