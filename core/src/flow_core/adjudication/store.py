"""StepStore implementations: persistent (DB) and in-memory.

Strategies write events through a ``StepStore`` rather than touching
the DB or any global state. Production wires ``DBStepStore`` against
the caller's tenant session (RLS-scoped); tests and composition
primitives use ``InMemoryStepStore``.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.adjudication.base import StepRecord
from flow_core.models.adjudication import AdjudicationStep, AdjudicationStepKind


class DBStepStore:
    """Append-only writer + reader for one adjudication, backed by the
    DB. Bound to a tenant ``AsyncSession`` so RLS applies on every
    insert/select."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        adjudication_id: uuid.UUID,
        org_id: uuid.UUID,
    ) -> None:
        self._session = session
        self._adjudication_id = adjudication_id
        self._org_id = org_id
        # Tracks the next step_no without an extra round-trip per
        # append. The constructor's first append discovers the current
        # max and seeds from there if the adjudication already has
        # steps (composition wrapping, retries).
        self._next_step_no: int | None = None

    async def _seed_step_no(self) -> int:
        stmt = select(AdjudicationStep.step_no).where(
            AdjudicationStep.adjudication_id == self._adjudication_id
        )
        existing = (await self._session.execute(stmt)).scalars().all()
        return (max(existing) + 1) if existing else 1

    async def append(
        self,
        *,
        kind: AdjudicationStepKind,
        payload: dict[str, Any],
        agent_id: str | None = None,
        embedding: Sequence[float] | None = None,
    ) -> int:
        if self._next_step_no is None:
            self._next_step_no = await self._seed_step_no()
        step_no = self._next_step_no
        self._next_step_no += 1

        row = AdjudicationStep(
            org_id=self._org_id,
            adjudication_id=self._adjudication_id,
            step_no=step_no,
            kind=kind,
            payload_json=payload,
            agent_id=agent_id,
            embedding=list(embedding) if embedding is not None else None,
            created_at=datetime.datetime.now(datetime.UTC),
        )
        self._session.add(row)
        await self._session.flush()
        return step_no

    async def list_steps(self) -> list[StepRecord]:
        stmt = (
            select(AdjudicationStep)
            .where(AdjudicationStep.adjudication_id == self._adjudication_id)
            .order_by(AdjudicationStep.step_no)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            StepRecord(
                step_no=r.step_no,
                kind=r.kind,
                payload=dict(r.payload_json),
                agent_id=r.agent_id,
                embedding=tuple(r.embedding) if r.embedding is not None else None,
            )
            for r in rows
        ]


class InMemoryStepStore:
    """Process-local store. Used by tests and inside composition
    primitives that need to virtualise a child strategy's view."""

    def __init__(self) -> None:
        self._steps: list[StepRecord] = []
        self._next_step_no = 1

    async def append(
        self,
        *,
        kind: AdjudicationStepKind,
        payload: dict[str, Any],
        agent_id: str | None = None,
        embedding: Sequence[float] | None = None,
    ) -> int:
        step_no = self._next_step_no
        self._next_step_no += 1
        self._steps.append(
            StepRecord(
                step_no=step_no,
                kind=kind,
                payload=dict(payload),
                agent_id=agent_id,
                embedding=tuple(embedding) if embedding is not None else None,
            )
        )
        return step_no

    async def list_steps(self) -> list[StepRecord]:
        return list(self._steps)
