"""One-shot, idempotent migration of in-DB attachment bytes to the
configured object store.

When a deployment switches ``MYCELIUM_ATTACHMENT_STORE`` from ``pg`` to
``s3``, legacy rows still carry their bytes in ``attachments.data``.
This streams every such row (``data IS NOT NULL AND storage_key IS
NULL``), uploads the bytes via the configured store, sets
``storage_key``, NULLs ``data``, and commits in bounded batches.

Idempotent and safe to re-run: an already-moved row (``storage_key``
set, ``data`` NULL) is skipped by the WHERE clause, so a crashed run
resumes cleanly. No-op when ``attachment_store == "pg"`` (nothing to
move) -- prints a clear message and exits 0. Fail-closed Settings (the
S3 validator rejects a half-configured target before any work).

Run: ``python -m mycelium_core.migrate_attachments`` (backend image in the
deploy; same DB URL + S3 config as the app). Structured logging.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid

from sqlalchemy import select

from mycelium_core.attachment_store import AttachmentStore, get_attachment_store
from mycelium_core.config import Settings, get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.attachment import Attachment
from mycelium_core.models.membership import Membership, Role
from mycelium_core.models.organization import Organization

logger = logging.getLogger("mycelium.migrate_attachments")

_DEFAULT_BATCH = 50


async def _all_org_ids() -> list[uuid.UUID]:
    """Every workspace, deterministic order. A global ``organizations``
    scan with the no-tenant admin session -- the same RLS-respecting
    cross-tenant pattern the P5 dispatch worker uses (it does NOT bypass
    RLS; org-scoped rows are then touched inside a tenant session)."""
    async with admin_session() as s:
        orgs = (await s.execute(select(Organization).order_by(Organization.id))).scalars().all()
    return [o.id for o in sorted(orgs, key=lambda o: str(o.id))]


async def _owner_of(org_id: uuid.UUID) -> uuid.UUID | None:
    """The earliest owner of the workspace (by ``created_at``,
    ``str(user_id)``), used as the actor for the tenant session so the
    org-scoped attachment writes satisfy RLS. ``None`` -> skip (an
    ownerless workspace, nothing to act as), like the dispatch worker."""
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
    return sorted(rows, key=lambda m: (m.created_at, str(m.user_id)))[0].user_id


async def _migrate_org(
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    store: AttachmentStore,
    batch_size: int,
) -> int:
    """Move one workspace's in-DB attachments, in bounded batches,
    inside its tenant session (RLS-scoped). Each batch commits on
    context exit; the next batch re-queries, so a crash mid-run resumes
    (idempotent: moved rows have ``data NULL`` and are no longer
    selected)."""
    moved = 0
    while True:
        async with tenant_session(str(org_id), str(actor_id)) as session:
            rows = (
                (
                    await session.execute(
                        select(Attachment)
                        .where(
                            Attachment.data.is_not(None),
                            Attachment.storage_key.is_(None),
                        )
                        .order_by(Attachment.id)
                        .limit(batch_size)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                break
            for att in rows:
                data = att.data
                if data is None:  # pragma: no cover - WHERE excludes this
                    continue
                key = str(att.id)
                await store.put(key, data, att.mime_type)
                att.storage_key = key
                att.data = None
                moved += 1
            logger.info(
                "org %s: migrated batch of %d (running total %d)",
                org_id,
                len(rows),
                moved,
            )
    return moved


async def migrate_attachments(
    *,
    store: AttachmentStore | None = None,
    settings: Settings | None = None,
    batch_size: int = _DEFAULT_BATCH,
) -> int:
    """Move every in-DB attachment to the object store. Returns the
    number of rows moved. Enumerates all workspaces and migrates each
    inside its own tenant session (RLS-respecting, the P5 dispatch-
    worker pattern), so it works under the app DB role without
    bypassing RLS. ``store``/``settings`` are injectable for tests;
    production resolves them from config (fail-closed)."""
    settings = settings or get_settings()
    if settings.attachment_store == "pg":
        logger.info("attachment_store=pg: nothing to migrate (bytes stay in the DB)")
        return 0
    store = store or get_attachment_store(settings)

    moved = 0
    for org_id in await _all_org_ids():
        owner = await _owner_of(org_id)
        if owner is None:
            logger.info("org %s has no owner; skipped", org_id)
            continue
        moved += await _migrate_org(org_id, owner, store, batch_size)

    logger.info("attachment migration complete: %d row(s) moved", moved)
    return moved


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("MYCELIUM_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    moved = asyncio.run(migrate_attachments())
    sys.stdout.write(f"attachment migration: {moved} row(s) moved\n")


if __name__ == "__main__":
    main()
