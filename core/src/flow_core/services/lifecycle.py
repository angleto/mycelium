"""Shared lifecycle transitions: archive / unarchive / soft_delete /
restore. Refactor of the near-identical pattern that lived in
``services/tasks._set`` and ``services/notes._note_set``: each domain
had its own boilerplate (require_role + optimistic_update + audit.log)
varying only in the model class + entity name. The per-domain
``archive_task / archive_note / ...`` wrappers stay as thin shims so
external call sites don't change.

The helper does NOT validate existence — callers are expected to do
that themselves (and usually want ``include_deleted=True`` semantics
the per-domain ``get_*`` already provides). Centralising the audit
trail keeps the diff format consistent across domains.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.concurrency import optimistic_update
from flow_core.models.membership import Role
from flow_core.services import audit
from flow_core.services.rbac import require_role


async def transition(
    session: AsyncSession,
    *,
    model_cls: type[Any],
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    entity_id: uuid.UUID,
    expected_version: int,
    values: dict[str, Any],
    audit_entity: str,
    audit_action: str,
    role_min: Role = Role.member,
) -> int:
    """Apply ``values`` to ``entity_id`` with optimistic concurrency,
    then write an audit row. Returns the new row version. Used by
    archive / soft_delete / restore / any other "flip a flag + audit"
    flow that doesn't need extra domain validation."""
    await require_role(session, org_id, actor_id, role_min)
    new_version = await optimistic_update(
        session,
        model_cls,
        pk=entity_id,
        expected_version=expected_version,
        values=values,
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity=audit_entity,
        entity_id=entity_id,
        action=audit_action,
        diff={k: str(v) for k, v in values.items()},
    )
    return new_version


def archived_values(archived: bool) -> dict[str, Any]:
    """The flip-the-flag value-map used by archive / unarchive."""
    return {"is_archived": archived}


def soft_delete_values() -> dict[str, Any]:
    """Mark the row deleted (UTC-now timestamp)."""
    return {"deleted_at": dt.datetime.now(tz=dt.UTC)}


def restore_values() -> dict[str, Any]:
    """Undo a soft-delete by clearing the timestamp."""
    return {"deleted_at": None}


__all__ = [
    "archived_values",
    "restore_values",
    "soft_delete_values",
    "transition",
]
