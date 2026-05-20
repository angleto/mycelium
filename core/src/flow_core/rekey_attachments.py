"""Rekey existing S3 attachments to the v1.2.1 hierarchical layout.

Pre-v1.2.1 the storage_key was a flat UUID (``str(att.id)``). v1.2.1
introduces a client-rooted hierarchy:

  org/<org>/client/<client_uid>/tasks/<task>/<att>/<filename>
  org/<org>/client/<client_uid>/notes/<note>/<att>/<filename>
  org/<org>/misc/<att>/<filename>          # no client / orphan

This one-shot script walks ``attachments`` rows whose storage_key is
NOT in the new shape, computes the correct key, copies the object in
S3 to the new key, updates the row, then deletes the old object.

Skips:
  - pg-backend rows (data is in-row; storage_key stays NULL).
  - rows already on a hierarchical key (idempotent).
  - rows whose object is missing in S3 (logged, never deleted).

Run inside a backend pod (or any environment that has FLOW_DATABASE_URL
and the S3 creds in env, exactly like the app):

  kubectl -n flow-production exec deploy/flow-backend -- \\
      python -m flow_core.rekey_attachments [--dry-run]

The script uses ``admin_session`` (no tenant GUC) so it sees every
org's rows; the storage_key build still threads org_id properly.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from sqlalchemy import select, text

from flow_core.attachment_store import (
    AttachmentStore,
    PgAttachmentStore,
    get_attachment_store,
)
from flow_core.config import get_settings
from flow_core.db import admin_session
from flow_core.models.attachment import Attachment
from flow_core.services.attachments import _build_storage_key, _resolve_client_tag_id


@dataclass
class Stats:
    inspected: int = 0
    skipped_pg: int = 0
    skipped_already_new: int = 0
    skipped_missing_object: int = 0
    rekeyed: int = 0
    errors: int = 0


def _is_new_shape(key: str) -> bool:
    return key.startswith("org/")


async def _rekey_one(att: Attachment, store: AttachmentStore, dry: bool) -> str | None:
    """Compute the new key and copy+swap+delete in S3. Returns the new
    key on success, None on a skip."""
    if att.storage_key is None or att.data is not None:
        return None
    if _is_new_shape(att.storage_key):
        return None

    # Resolve client via admin_session (no tenant context) — must run
    # against the org the row belongs to. We pass org_id to _resolve_*
    # via a per-task/per-note org SELECT done inside that helper's
    # queries (they JOIN on TaskTag/NoteTag with implicit org via the
    # FK + the admin session sees all). The helper does not actually
    # need a GUC: its queries are JOINs by id, not RLS-filtered selects.
    async with admin_session() as s:
        client_tag_id = await _resolve_client_tag_id(s, task_id=att.task_id, note_id=att.note_id)
    parent_kind = "tasks" if att.task_id is not None else "notes"
    parent_id = att.task_id or att.note_id or att.id
    new_key = _build_storage_key(
        org_id=att.org_id,
        client_tag_id=client_tag_id,
        parent_kind=parent_kind,
        parent_id=parent_id,
        attachment_id=att.id,
        filename=att.filename,
    )
    if dry:
        return new_key

    # S3 copy + delete. Use the existing store API (get -> put -> delete)
    # rather than a server-side copy because the S3AttachmentStore
    # already wraps boto3 with the correct creds + endpoint.
    blob = await store.get(att.storage_key)
    await store.put(new_key, blob, att.mime_type)
    return new_key


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    settings = get_settings()
    store = get_attachment_store(settings)
    if isinstance(store, PgAttachmentStore):
        print("Attachment backend is 'pg' — nothing to rekey, exiting.")
        return

    stats = Stats()
    async with admin_session() as s:
        rows = (
            (await s.execute(select(Attachment).where(Attachment.storage_key.is_not(None))))
            .scalars()
            .all()
        )
    print(f"Inspecting {len(rows)} S3-backed attachments...")

    for att in rows:
        stats.inspected += 1
        if att.data is not None:
            stats.skipped_pg += 1
            continue
        if att.storage_key is None:
            stats.skipped_pg += 1
            continue
        if _is_new_shape(att.storage_key):
            stats.skipped_already_new += 1
            continue
        try:
            new_key = await _rekey_one(att, store, args.dry_run)
        except FileNotFoundError:
            print(f"  MISSING in S3: {att.id} ({att.storage_key})")
            stats.skipped_missing_object += 1
            continue
        except Exception as e:
            print(f"  ERROR on {att.id}: {e!r}")
            stats.errors += 1
            continue
        if new_key is None:
            stats.skipped_already_new += 1
            continue
        if args.dry_run:
            print(f"  [dry] {att.storage_key}  ->  {new_key}")
            stats.rekeyed += 1
            continue
        # Update the row + delete the old object in one atomic-ish step:
        # first persist the new key (so a crash after rename still has a
        # findable row), then delete the old S3 object.
        async with admin_session() as s:
            await s.execute(
                text("UPDATE attachments SET storage_key = :k WHERE id = :i"),
                {"k": new_key, "i": str(att.id)},
            )
            await s.commit()
        old = att.storage_key
        await store.delete(old)
        print(f"  rekeyed {att.id}: {old}  ->  {new_key}")
        stats.rekeyed += 1

    print(
        f"\nDone. inspected={stats.inspected} rekeyed={stats.rekeyed} "
        f"already_new={stats.skipped_already_new} "
        f"missing_in_s3={stats.skipped_missing_object} "
        f"errors={stats.errors}"
    )


if __name__ == "__main__":
    asyncio.run(main())
