"""Backfill of the written-reference projection, with its report (ADR-0073
decision 9).

Projects every note part and task description that has no projection yet,
or whose projection does not match its text, in every workspace, then
prints the corpus-wide measurement: occurrences by syntax and status, and
every ambiguous and dangling occurrence with its source, so a person can
resolve the first and look at the second.

Idempotent: a second run finds nothing to project and prints the same
counts. Occurrences found here have ``first_seen_at`` NULL. The worker's
hourly repair (``mycelium_worker.body_references``) reaches the same state
on its own; this command exists to run it at once and to read the report.

Workspaces are enumerated the way ``normalize_note_uuids`` does, through the
non-org-scoped ``users`` table and the SECURITY DEFINER
``list_user_organizations``, and each one is processed inside its own tenant
session, so RLS applies throughout.

Run (backend image, same DB URL as the app)::

    python -m mycelium_core.backfill_body_references
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select

from mycelium_core.db import tenant_session
from mycelium_core.models.body_reference import BodyReference, BodyReferenceSource
from mycelium_core.models.note_part import NotePart
from mycelium_core.normalize_note_uuids import (
    _orgs_with_actor,  # pyright: ignore[reportPrivateUsage]
)
from mycelium_core.services import body_references as refs_svc

logger = logging.getLogger("mycelium.backfill_body_references")


@dataclass
class OrgReport:
    org_id: uuid.UUID
    projected_parts: int = 0
    projected_tasks: int = 0
    by_syntax_status: Counter[tuple[str, str]] = field(default_factory=Counter)
    unresolved: list[str] = field(default_factory=list)


async def backfill_org(org_id: uuid.UUID, actor_id: uuid.UUID) -> OrgReport:
    report = OrgReport(org_id=org_id)
    async with tenant_session(str(org_id), str(actor_id), actor_kind="system") as s:
        repaired = await refs_svc.repair_drift(s, org_id=org_id)
        report.projected_parts = repaired.parts
        report.projected_tasks = repaired.tasks
    async with tenant_session(str(org_id), str(actor_id), actor_kind="system") as s:
        rows = (
            await s.execute(
                select(
                    BodyReference.syntax,
                    BodyReference.status,
                    BodyReference.token,
                    BodyReferenceSource.task_id,
                    NotePart.note_id,
                )
                .join(BodyReferenceSource, BodyReferenceSource.id == BodyReference.source_id)
                .outerjoin(NotePart, NotePart.id == BodyReferenceSource.note_part_id)
                .where(BodyReference.org_id == org_id)
                .order_by(BodyReference.status, BodyReference.token)
            )
        ).all()
    for syntax, status, token, task_id, note_id in rows:
        report.by_syntax_status[(syntax, status)] += 1
        if status != "resolved":
            where = f"note {note_id}" if note_id is not None else f"task {task_id}"
            report.unresolved.append(f"{status:9} {syntax:7} {token:36} in {where}")
    return report


def render(reports: list[OrgReport]) -> str:
    lines: list[str] = []
    for r in reports:
        lines.append(
            f"workspace {r.org_id}: projected {r.projected_parts} part(s), "
            f"{r.projected_tasks} task description(s)"
        )
        total = sum(r.by_syntax_status.values())
        lines.append(f"  occurrences: {total}")
        for (syntax, status), n in sorted(r.by_syntax_status.items()):
            lines.append(f"    {syntax:7} {status:9} {n}")
        if r.unresolved:
            lines.append("  ambiguous and dangling:")
            lines.extend(f"    {u}" for u in r.unresolved)
    return "\n".join(lines) + "\n"


async def backfill_all() -> list[OrgReport]:
    org_actor = await _orgs_with_actor()
    logger.info("enumerated %d workspace(s)", len(org_actor))
    return [await backfill_org(org_id, actor) for org_id, actor in org_actor.items()]


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("MYCELIUM_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    sys.stdout.write(render(asyncio.run(backfill_all())))


if __name__ == "__main__":
    main()
