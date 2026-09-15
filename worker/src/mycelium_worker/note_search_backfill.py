"""Note-search backfill loop: two passes over the back-catalogue.

Indexes note parts that pre-date the per-part index deploy (migration
0040): the listener-driven resync catches every new mutation, but parts
created before the deploy never went through it, so an old note stays
invisible to semantic retrieval until it is edited. This sweep walks
those parts and runs the same ``_resync_part_blob`` the listener would
have, so the back-catalogue becomes searchable on its own.

The second pass (migration 0016) is the same repair one deploy later: a
part indexed before the indexer chunked anything holds ONE blob, and
above the chunker threshold that blob is the head of the part presented
as the whole of it. It re-indexes those through the same resync, which is
the only path that keeps the pointer set and the blob set one thing.
Both passes are idempotent and neither re-embeds a part it has already
finished with.

Unlike ``task_search_backfill`` there is no separate embedding-backfill
call here: keyword-only blobs (embedder timed out) are re-embedded
generically by the ``embedding_migration`` worker, which re-embeds any
blob with a NULL vector regardless of channel.

Per-workspace, exception-isolated, same shape as ``task_search_backfill``:
enumerate orgs under ``admin_session``, then run each backfill tick inside
a ``tenant_session`` so RLS scopes the SELECT and the owner is a real
member with role >= member (the service-level call is system-actor).
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_checkpoint, tenant_session
from mycelium_core.models.membership import Membership, Role
from mycelium_core.models.organization import Organization
from mycelium_core.services import note_search

_log = logging.getLogger("mycelium.worker.note_search")


async def _all_workspaces() -> list[uuid.UUID]:
    async with admin_session() as s:
        orgs = (await s.execute(select(Organization).order_by(Organization.id))).scalars().all()
        return [o.id for o in sorted(orgs, key=lambda o: str(o.id))]


async def _owner_of(org_id: uuid.UUID) -> uuid.UUID | None:
    async with admin_session() as s:
        rows = (
            (
                await s.execute(
                    select(Membership)
                    .where(Membership.org_id == org_id, Membership.role == Role.owner)
                    .order_by(Membership.created_at, Membership.user_id)
                )
            )
            .scalars()
            .all()
        )
    if not rows:
        return None
    ordered = sorted(rows, key=lambda m: (m.created_at, str(m.user_id)))
    return ordered[0].user_id


# How many parts one sweep will touch in a workspace before leaving the
# rest to the next tick. A ceiling on the WORK, not on the transaction:
# the transaction is one part wide (see _drain), so this only decides how
# long a single tick runs, and both passes end on their own once the
# back-catalogue is drained.
_PARTS_PER_TICK = 200


async def _drain(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    parts_per_tick: int = _PARTS_PER_TICK,
) -> tuple[int, int]:
    """Both passes, ONE PART PER TRANSACTION, committing in between.

    The transaction boundary is the whole point of this function, and it
    is here rather than inside the services because the caller owns the
    transaction and a service that commits cannot be composed into one.

    What it is for, measured on 2026-09-15: the first tick after the
    2.3.36 rollout rechunked 171 parts inside a SINGLE transaction and
    held it for about thirteen minutes. In the same hour the migration of
    that release lost its lock race twice, and the cause was a worker
    holding long transactions on the same table -- the migration lifts
    FORCE ROW LEVEL SECURITY with an ACCESS EXCLUSIVE per table under a 5
    second lock_timeout, so anything long on ``memory_blobs`` beats it.
    The sweep was about to become a second permanent source of exactly
    that, on every worker restart for as long as there was a
    back-catalogue.

    One part is the right unit and not an arbitrary small number: the
    embed budget in ``note_search`` is granted PER PART and is 2 seconds,
    so a one-part transaction cannot outlive the 5 second lock_timeout,
    while a five-part one could.

    ``tenant_checkpoint`` and not a new session per part: it commits and
    re-arms the tenant GUCs on the same session, which is the primitive
    the two-phase transmit already uses for the same reason (release the
    locks, keep working). It also runs the search-dirty flush hooks
    first, exactly as the end of ``tenant_session`` does.

    A second property comes free and matters as much: progress is durable
    per part. Before this, a worker restart in the middle of a sweep lost
    every part it had done.
    """
    indexed = 0
    rechunked = 0
    after: uuid.UUID | None = None

    while indexed + rechunked < parts_per_tick:
        did = await note_search.run_pointer_backfill(session, batch_size=1)
        if not did:
            break
        indexed += did
        await tenant_checkpoint(session)

    while indexed + rechunked < parts_per_tick:
        sweep = await note_search.run_rechunk_backfill(session, batch_size=1, after_id=after)
        if sweep.last_examined is None:
            break
        # The cursor moves on what was EXAMINED, not on what was
        # rechunked, so a part the word count declines is stepped over
        # rather than being the only part this tick ever sees.
        after = sweep.last_examined
        rechunked += sweep.rechunked
        if sweep.rechunked:
            await tenant_checkpoint(session)

    _log.debug("note-search drain org=%s indexed=%d rechunked=%d", org_id, indexed, rechunked)
    return indexed, rechunked


async def run_once(parts_per_tick: int = _PARTS_PER_TICK) -> int:
    """One sweep across all workspaces, both passes.

    Returns the parts touched: those that pre-date the per-part index
    deploy (no pointer yet), plus those indexed as a single whole-doc
    blob while long enough to chunk. Per-workspace exceptions
    isolated/logged.
    """
    try:
        org_ids = await _all_workspaces()
    except Exception:
        _log.exception("note-search backfill: failed to list workspaces")
        return 0
    total = 0
    for org_id in org_ids:
        try:
            owner = await _owner_of(org_id)
            if owner is None:
                continue
            async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
                indexed, rechunked = await _drain(s, org_id=org_id, parts_per_tick=parts_per_tick)
            if indexed or rechunked:
                _log.info(
                    "note-search backfill org=%s indexed=%d rechunked=%d",
                    org_id,
                    indexed,
                    rechunked,
                )
            total += indexed + rechunked
        except Exception:
            _log.exception("note-search backfill failed for org=%s", org_id)
    return total


async def run_forever() -> None:
    interval = max(5, get_settings().note_search_backfill_interval_seconds)
    _log.info("note-search backfill worker started (interval=%ds)", interval)
    # No boost tick any more, and its absence is the fix rather than a
    # simplification. The boost was a batch of 500 handed to BOTH passes,
    # and the second costs up to sixteen embeds a part against the
    # first's one; it is what made the 2026-09-15 sweep a thirteen-minute
    # transaction. The drain above replaces it and is strictly better: it
    # keeps going until the back-catalogue is empty instead of stopping
    # at a number, and it commits after every part, so the ceiling it
    # respects is on work done and never on locks held.
    while True:
        await run_once()
        await asyncio.sleep(interval)
