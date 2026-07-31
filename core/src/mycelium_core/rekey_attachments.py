"""Rekey S3 attachments onto the client-rooted storage layout.

The key an attachment must live at is the one
``services/attachments._build_storage_key`` derives from the row's org,
its parent (task XOR note) and the CLIENT that parent belongs to:

  org/<org>/client/<client_tag>/tasks/<task>/<att>/<filename>
  org/<org>/client/<client_tag>/notes/<note>/<att>/<filename>
  org/<org>/misc/<att>/<filename>          # parent with no client

Two things put a row out of that layout. A pre-hierarchy row still
carries the flat ``str(att.id)`` key. And a row whose parent CHANGED
CLIENT after the upload keeps its object under the PREVIOUS client's
prefix: migration 0086 moved entities to another client while
repairing the structural-tag invariant (ADR-0050), and re-pointing a
task at another project does the same at runtime. The second case is
the one that bites, because one client's folder then holds another
client's file.

So this script does not reason about the SHAPE of the stored key: a
``startswith("org/")`` test calls a hierarchical key under the WRONG
client "already correct" and is exactly why the wrong-client case went
unseen. For every object-store-backed row it recomputes the key the row
SHOULD have and moves the object whenever the stored key differs. That
subsumes the flat-legacy case, and it is idempotent by construction --
a second run recomputes the same key and finds the row already on it.

Skips:
  - pg-backend rows (bytes are in-row; storage_key stays NULL);
  - rows already on their expected key;
  - rows whose object is missing in the store (logged, never deleted).

RUNNING IT. Nothing beyond the pod's own environment is needed:

  kubectl -n mycelium-production exec deploy/mycelium-backend -- \\
      python -m mycelium_core.rekey_attachments [--dry-run]

The scan walks workspace by workspace and does its per-row work inside
a ``tenant_session``, the shape ``migrate_attachments`` already uses on
this same table. That is not a stylistic choice: a single cross-tenant
``admin_session`` scan never sets ``app.current_org``, and the
``attachments`` policy is
``org_id = NULLIF(current_setting('app.current_org', true), '')::uuid``,
so under the ordinary runtime role it matches NOTHING -- the tool would
print ``inspected=0``, exit 0, and read as a clean run while having
looked at nothing. Workspaces are enumerated through the
``organizations`` system-session policy (migration 0029); no superuser
and no BYPASSRLS role is involved.

Run it with --dry-run first: it prints every move it would make and
touches neither the database nor the object store.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.attachment_store import (
    AttachmentStore,
    PgAttachmentStore,
    get_attachment_store,
)
from mycelium_core.config import get_settings
from mycelium_core.db import tenant_session
from mycelium_core.migrate_attachments import _all_org_ids, _owner_of
from mycelium_core.models.attachment import Attachment
from mycelium_core.services.attachments import _build_storage_key, _resolve_client_tag_id


@dataclass
class Stats:
    inspected: int = 0
    skipped_pg: int = 0
    # Already on the key it should have: the ONLY skip that means
    # "correct". Kept apart from skipped_pg (no object to move at all)
    # so a run's summary still says which of the two happened.
    skipped_correct: int = 0
    skipped_missing_object: int = 0
    rekeyed: int = 0
    errors: int = 0


async def _expected_key(session: AsyncSession, att: Attachment) -> str:
    """The key ``services/attachments`` would build for this row today.

    The client is not on the attachment row: it hangs off the parent's
    junction rows (ADR-0050), so answering "is this key still correct?"
    costs one client lookup per attachment -- one SELECT, two when the
    client comes from the project -- where the discarded shape test was
    free. That is the price of being able to see a wrong-client key at
    all, and this is an occasional ops scan, not a request path, so it
    is paid per row instead of hidden behind a batching layer.

    ``att.filename`` was sanitised on write, so what comes back is
    exactly what the upload path would produce today.
    """
    client_tag_id = await _resolve_client_tag_id(session, task_id=att.task_id, note_id=att.note_id)
    parent_kind = "tasks" if att.task_id is not None else "notes"
    # The table CHECK enforces note XOR task, so one of the two is set;
    # the attachment id is a type-checker fallback, never reached.
    parent_id = att.task_id or att.note_id or att.id
    return _build_storage_key(
        org_id=att.org_id,
        client_tag_id=client_tag_id,
        parent_kind=parent_kind,
        parent_id=parent_id,
        attachment_id=att.id,
        filename=att.filename,
    )


async def _copy_object(
    store: AttachmentStore, *, old_key: str, new_key: str, content_type: str
) -> None:
    """Copy the object to ``new_key``, leaving the old one in place (the
    caller deletes it only after the row points at the new key).

    get -> put through the store API rather than a server-side S3 copy:
    ``S3AttachmentStore`` already wraps boto3 with the right endpoint,
    credentials and key prefix, and there is no second place to keep
    that wiring correct.
    """
    blob = await store.get(old_key)
    await store.put(new_key, blob, content_type)


async def _rows_of_org(org_id: uuid.UUID, actor_id: uuid.UUID) -> list[Attachment]:
    """One workspace's object-store-backed rows, read inside its tenant
    session so the RLS predicate is satisfied. Detached from the session
    on return: the caller only needs the scalar fields, and holding a
    transaction open across the object-store round trips would be worse
    than re-opening one per write."""
    async with tenant_session(str(org_id), str(actor_id)) as s:
        rows = (
            (
                await s.execute(
                    select(Attachment)
                    .where(Attachment.storage_key.is_not(None))
                    .order_by(Attachment.id)
                )
            )
            .scalars()
            .all()
        )
        for att in rows:
            s.expunge(att)
        return list(rows)


async def rekey_attachments(
    store: AttachmentStore,
    *,
    dry_run: bool = False,
    org_ids: Sequence[uuid.UUID] | None = None,
) -> Stats:
    """Move every object-store-backed attachment whose stored key is not
    the key it should have. Returns the tally.

    Iterates workspace by workspace inside a ``tenant_session``, the same
    RLS-respecting shape ``migrate_attachments`` uses. A single
    cross-tenant scan under ``admin_session`` does NOT work here: that
    session never sets ``app.current_org`` and the ``attachments`` policy
    is ``org_id = NULLIF(current_setting('app.current_org', true), '')``,
    so under the runtime role it matches zero rows and the tool reports
    success having inspected nothing.
    """
    stats = Stats()
    rows: list[tuple[uuid.UUID, uuid.UUID, Attachment]] = []
    # ``org_ids`` narrows the sweep to named workspaces. Migration 0086
    # prints the entities whose client moved, and they all belong to one
    # workspace, so re-keying after a repair does not need to walk every
    # tenant. It also makes a run assertable: a caller can tally its own
    # workspace instead of whatever else the database happens to hold.
    targets = list(org_ids) if org_ids is not None else await _all_org_ids()
    for org_id in targets:
        owner = await _owner_of(org_id)
        if owner is None:
            # Ownerless workspace: no actor to open a tenant session as,
            # so nothing can be read or written for it. Same skip the
            # dispatch worker and migrate_attachments make.
            continue
        for att in await _rows_of_org(org_id, owner):
            rows.append((org_id, owner, att))
    print(f"Inspecting {len(rows)} object-store-backed attachments...")

    for org_id, owner, att in rows:
        stats.inspected += 1
        # pg-backend row: the bytes live in the row, so there is no
        # object to move. The query above already filters storage_key
        # NOT NULL; restating it here also narrows the type below.
        if att.storage_key is None or att.data is not None:
            stats.skipped_pg += 1
            continue
        try:
            # One session + one client lookup per row (see _expected_key);
            # closed again before the store round trip, so no DB
            # transaction is held open across the network.
            async with tenant_session(str(org_id), str(owner)) as s:
                expected = await _expected_key(s, att)
        except Exception as e:
            print(f"  ERROR resolving the client of {att.id}: {e!r}")
            stats.errors += 1
            continue
        if att.storage_key == expected:
            stats.skipped_correct += 1
            continue
        if dry_run:
            print(f"  [dry] {att.storage_key}  ->  {expected}")
            stats.rekeyed += 1
            continue
        try:
            await _copy_object(
                store,
                old_key=att.storage_key,
                new_key=expected,
                content_type=att.mime_type,
            )
        except (FileNotFoundError, KeyError):
            # A missing object is a data-loss report, not a rekey: name
            # the row and delete nothing. The store Protocol does not
            # normalise "not found" (the in-memory store raises
            # KeyError, boto3 raises ClientError/NoSuchKey, which lands
            # in the generic bucket below), so both spellings this
            # module can name without importing boto3 are caught here.
            print(f"  MISSING in the object store: {att.id} ({att.storage_key})")
            stats.skipped_missing_object += 1
            continue
        except Exception as e:
            print(f"  ERROR on {att.id}: {e!r}")
            stats.errors += 1
            continue
        # Persist the new key BEFORE deleting the old object: a crash in
        # between leaves an orphan copy under the old key (harmless, and
        # the next run is a no-op), never a row pointing at nothing.
        async with tenant_session(str(org_id), str(owner)) as s:
            await s.execute(
                text("UPDATE attachments SET storage_key = :k WHERE id = :i"),
                {"k": expected, "i": str(att.id)},
            )
        old = att.storage_key
        await store.delete(old)
        print(f"  rekeyed {att.id}: {old}  ->  {expected}")
        stats.rekeyed += 1

    return stats


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--org",
        action="append",
        default=None,
        metavar="UUID",
        help="Limit the sweep to this workspace; repeatable. Default: every workspace.",
    )
    args = ap.parse_args()

    settings = get_settings()
    store = get_attachment_store(settings)
    if isinstance(store, PgAttachmentStore):
        print("Attachment backend is 'pg' -- nothing to rekey, exiting.")
        return

    org_ids = [uuid.UUID(o) for o in args.org] if args.org else None
    stats = await rekey_attachments(store, dry_run=args.dry_run, org_ids=org_ids)
    moved = "would_rekey" if args.dry_run else "rekeyed"
    print(
        f"\nDone. inspected={stats.inspected} {moved}={stats.rekeyed} "
        f"already_correct={stats.skipped_correct} "
        f"pg_backed={stats.skipped_pg} "
        f"missing_in_store={stats.skipped_missing_object} "
        f"errors={stats.errors}"
    )


if __name__ == "__main__":
    asyncio.run(main())
