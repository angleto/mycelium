"""Periodic autonomous email responder worker (WS-4).

The scheduled half of the responder: on a modest interval, drain the
``email_responder_jobs`` queue and draft a reply per job. Self-gated on
``settings.email_responder_enabled`` (no-op when off, like the Telegram
assistant). Session/enumeration mirrors ``email_sync`` exactly: a worker has
no API ``tenant_ctx``, so we enumerate workspaces with the no-tenant
``admin_session`` (system-actor window) and act inside a per-org
``tenant_session(org, owner, actor_kind="system")`` where RLS is enforced.

Claiming (mark ``running``) and drafting run in SEPARATE per-org sessions so
a slow model call never holds the queue row lock. Drafts are WITHHELD in
state ``drafted``; nothing is ever sent without a human approving it.

Robustness: each workspace is isolated (one failure never stops the rest)
and the whole pass is wrapped so a transient error never kills the loop.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.membership import Membership, Role
from mycelium_core.models.organization import Organization
from mycelium_core.services import email_responder as responder_svc

_log = logging.getLogger("mycelium.worker.email_responder")

# Per-workspace claim batch. Modest: drafting is a multi-second LLM call and
# the queue drains over successive ticks.
_CLAIM_LIMIT = 10


async def _all_workspaces() -> list[uuid.UUID]:
    """Every workspace gets a responder tick (the presence of queued jobs is
    a tenant-scoped fact). Deterministic order. Read with the no-tenant admin
    session (global ``organizations`` scan, system-actor window)."""
    async with admin_session() as s:
        orgs = (await s.execute(select(Organization).order_by(Organization.id))).scalars().all()
        return [o.id for o in sorted(orgs, key=lambda o: str(o.id))]


async def _owner_of(org_id: uuid.UUID) -> uuid.UUID | None:
    """The workspace owner the worker acts as (deterministic: earliest owner
    membership). ``None`` when there is no owner. Mirrors
    ``mycelium_worker.email_sync._owner_of``."""
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


async def run_once() -> int:
    """One sweep: claim + draft per workspace. Returns the number of jobs
    drafted (for logging / tests). Per-workspace exceptions are isolated."""
    if not get_settings().email_responder_enabled:
        return 0
    drafted = 0
    try:
        org_ids = await _all_workspaces()
    except Exception:  # transient DB error: skip this sweep, keep looping
        _log.exception("email responder sweep: failed to list workspaces")
        return 0
    for org_id in org_ids:
        try:
            owner = await _owner_of(org_id)
            if owner is None:
                continue
            async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
                job_ids = await responder_svc.claim_pending(s, limit=_CLAIM_LIMIT)
            for job_id in job_ids:
                async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
                    status = await responder_svc.draft_job(
                        s, org_id=org_id, actor_id=owner, job_id=job_id
                    )
                if status == "drafted":
                    drafted += 1
            if job_ids:
                _log.info("email responder org=%s drafted=%d", org_id, drafted)
        except Exception:  # one workspace failing must not stop the rest
            _log.exception("email responder failed for org=%s", org_id)
    return drafted


async def run_forever() -> None:
    """The periodic loop: ``run_once`` every
    ``settings.email_responder_loop_interval_seconds``. No-op (returns) when
    the responder is disabled, like the Telegram assistant; never exits on a
    per-sweep error."""
    settings = get_settings()
    if not settings.email_responder_enabled:
        _log.info("email responder worker disabled (MYCELIUM_EMAIL_RESPONDER_ENABLED=false)")
        return
    interval = max(15, settings.email_responder_loop_interval_seconds)
    _log.info("email responder worker started (interval=%ds)", interval)
    while True:
        try:
            await run_once()
        except Exception:
            _log.exception("email responder loop tick failed")
        await asyncio.sleep(interval)
