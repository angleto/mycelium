"""LLM-generated labels for the recovery-history timeline.

Sweep ``entity_revision`` rows whose ``summary`` is still NULL,
generate a short "speaking name" via the configured open-model LLM
(``ai_providers.get_llm()``; in production an in-cluster Ollama
provider, wired in ``worker/main.py``), and persist via
``entity_revisions.set_summary``. Oldest sealed rows first so the
timeline becomes readable from the earliest gap onwards.

No-op when ``LocalLLM`` is the active provider: ``LocalLLM.complete``
raises a ``RuntimeError`` (it's a stub, not a real backend), and
each per-revision attempt swallows that exception independently so
one bad row never blocks the sweep. The 30s tick is slow on
purpose: each generation is a multi-second LLM call.

Per-tenant exception isolation mirrors the other sweep jobs
(``reminders``, ``embedding_migration``, ``revisions``).
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select

from flow_core.ai_providers import get_llm
from flow_core.config import get_settings
from flow_core.db import admin_session, tenant_session
from flow_core.models.entity_revision import EntityRevision
from flow_core.models.membership import Membership, Role
from flow_core.models.organization import Organization
from flow_core.services import entity_revisions as revisions_svc

_log = logging.getLogger("flow.worker.revisions_summary")

_SYSTEM_PROMPT = (
    "You write short labels (4 to 10 words) summarizing an edit to a "
    "note or task. Use the same language as the input title/body. "
    "No quotes, no trailing punctuation, no prefix like 'Edit:' or "
    "'Update:'. Be concrete and refer to the change, not the entity "
    "type."
)


def _build_user_prompt(rev: EntityRevision) -> str:
    snap = rev.snapshot or {}
    fields = ", ".join(rev.changed_fields or []) or "(unknown)"
    title = snap.get("title") or ""
    body: str
    if rev.entity_kind == "note":
        body = (snap.get("transcript") or snap.get("summary") or "")[:600]
    else:
        body = (snap.get("description") or snap.get("summary") or "")[:600]
    parts: list[str] = [
        f"entity_kind: {rev.entity_kind}",
        f"changed_fields: {fields}",
    ]
    if title:
        parts.append(f"title: {title}")
    if body:
        parts.append(f"body: {body}")
    return "\n".join(parts)


async def _all_workspaces() -> list[uuid.UUID]:
    async with admin_session() as s:
        rows = (await s.execute(select(Organization).order_by(Organization.id))).scalars().all()
        return [o.id for o in sorted(rows, key=lambda o: str(o.id))]


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
    return rows[0].user_id


async def _summarize_org(org_id: uuid.UUID, owner_id: uuid.UUID, batch: int) -> int:
    """Generate summaries for up to ``batch`` pending revisions in one
    workspace. Each generation is independent: a failure on one row
    is logged and the loop continues."""
    llm = get_llm()
    filled = 0
    async with tenant_session(str(org_id), str(owner_id), actor_kind="system") as s:
        pending = await revisions_svc.list_pending_summaries(s, limit=batch)
        for rev in pending:
            try:
                user_prompt = _build_user_prompt(rev)
                result = await llm.complete(
                    system=_SYSTEM_PROMPT,
                    messages=[("user", user_prompt)],
                )
                label = (result.text or "").strip()
                if not label:
                    continue
                await revisions_svc.set_summary(
                    s,
                    revision_id=rev.id,
                    summary=label,
                    entity_kind=rev.entity_kind,
                    entity_id=rev.entity_id,
                )
                filled += 1
            except Exception:
                _log.exception("revisions summary failed org=%s rev=%s", org_id, rev.id)
    return filled


async def run_once(batch: int = 5) -> int:
    """One sweep across all workspaces. Returns total summaries
    generated. Per-workspace exception isolation."""
    total = 0
    try:
        org_ids = await _all_workspaces()
    except Exception:
        _log.exception("revisions summary: failed to list workspaces")
        return 0
    for org_id in org_ids:
        try:
            owner = await _owner_of(org_id)
            if owner is None:
                continue
            count = await _summarize_org(org_id, owner, batch)
            if count:
                _log.info("revisions summary org=%s labelled=%d", org_id, count)
            total += count
        except Exception:
            _log.exception("revisions summary failed for org=%s", org_id)
    return total


async def run_forever() -> None:
    s = get_settings()
    interval = max(5, s.revisions_summary_interval_seconds)
    batch = max(1, s.revisions_summary_batch)
    _log.info(
        "revisions summary worker started (interval=%ds, batch=%d, model=%s)",
        interval,
        batch,
        s.open_model or "<not configured>",
    )
    while True:
        await run_once(batch=batch)
        await asyncio.sleep(interval)
