"""Atomic idempotency claim for the public Invoice API (task 19b7e874).

The claim is an ``INSERT ... ON CONFLICT DO NOTHING`` on
``(issuer_profile_id, endpoint, idempotency_key)`` run in the SAME transaction as
the mutation, so a retry never files a second fiscal document:

- first caller: the insert succeeds (``is_new``); it does the mutation, stores the
  response snapshot, and commits.
- a concurrent retry with the same key: its insert blocks on the unique index
  until the first commits, then sees the conflict (0 rows) and the committed row
  with its snapshot -> it REPLAYS instead of mutating.

A reuse of the key with a different request body is a 422; a claim that exists but
has no snapshot yet (a still-in-flight or crashed sibling) is a 409 -- UNLESS the
claim already carries its invoice: the two-phase transmit (ADR-0046) commits the
claim together with the pre-dispatch invoice state, so a snapshot-less claim with
an ``invoice_id`` marks an unsettled DISPATCH, not a live sibling. The retry then
RESUMES the same invoice's transmission (``resume_invoice_id``) instead of failing
or composing a duplicate; the invoice-level dispatch lease arbitrates concurrency.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import ConflictError, UnprocessableError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.api_idempotency import ApiIdempotency


def request_digest(payload: Any) -> bytes:
    """Stable sha256 of the request, key-order-independent, so a semantically
    identical retry hashes the same and a materially different body does not."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).digest()


@dataclass(frozen=True, slots=True)
class Claim:
    is_new: bool
    row_id: uuid.UUID | None
    replay: dict[str, Any] | None
    # Set when a prior attempt with this key committed its pre-dispatch state
    # but never settled (ADR-0046): the caller must RESUME transmitting this
    # invoice instead of re-executing the mutation from scratch.
    resume_invoice_id: uuid.UUID | None = None


async def claim(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    issuer_profile_id: uuid.UUID,
    endpoint: str,
    idempotency_key: str,
    request_hash: bytes,
) -> Claim:
    ins = (
        pg_insert(ApiIdempotency)
        .values(
            org_id=org_id,
            issuer_profile_id=issuer_profile_id,
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        .on_conflict_do_nothing(index_elements=["issuer_profile_id", "endpoint", "idempotency_key"])
        .returning(ApiIdempotency.id)
    )
    new_id = (await session.execute(ins)).scalar_one_or_none()
    if new_id is not None:
        return Claim(is_new=True, row_id=new_id, replay=None)
    existing = (
        await session.execute(
            select(ApiIdempotency).where(
                ApiIdempotency.issuer_profile_id == issuer_profile_id,
                ApiIdempotency.endpoint == endpoint,
                ApiIdempotency.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one()
    if existing.request_hash != request_hash:
        raise UnprocessableError(MessageCode.IDEMPOTENCY_BODY_MISMATCH)
    if existing.response_snapshot is None:
        if existing.invoice_id is not None:
            # The prior attempt's pre-dispatch commit (ADR-0046) persisted the
            # claim with its invoice: resume that invoice's transmission (the
            # dispatch lease 409s a still-live sibling; an expired one retries).
            return Claim(
                is_new=False,
                row_id=existing.id,
                replay=None,
                resume_invoice_id=existing.invoice_id,
            )
        raise ConflictError(MessageCode.IDEMPOTENCY_IN_PROGRESS)
    return Claim(is_new=False, row_id=existing.id, replay=existing.response_snapshot)


async def attach_invoice(
    session: AsyncSession, *, row_id: uuid.UUID, invoice_id: uuid.UUID
) -> None:
    """Bind the claim to its invoice BEFORE the dispatch, so the pre-dispatch
    commit (ADR-0046 phase 1) persists the pair atomically and a retry after
    a crash/unsettled dispatch can resume instead of double-composing."""
    await session.execute(
        update(ApiIdempotency).where(ApiIdempotency.id == row_id).values(invoice_id=invoice_id)
    )


async def store(
    session: AsyncSession,
    *,
    row_id: uuid.UUID,
    snapshot: dict[str, Any],
    invoice_id: uuid.UUID | None = None,
) -> None:
    await session.execute(
        update(ApiIdempotency)
        .where(ApiIdempotency.id == row_id)
        .values(response_snapshot=snapshot, invoice_id=invoice_id)
    )


__all__ = ["Claim", "attach_invoice", "claim", "request_digest", "store"]
