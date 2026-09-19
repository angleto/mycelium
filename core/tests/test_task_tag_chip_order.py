"""The order a task's tag chips come back in (``tasks.tags_by_task``).

Order is a contract here, not a nicety, because more than one surface
renders only the FIRST few chips and counts the rest: a list row shows
three and a "+N". Two things follow. The chips have to arrive in the
same order on every read -- the join has no inherent one, so without an
ORDER BY the same page could show a different three after a replan --
and the ones worth the width have to arrive first: a task is exactly one
project and exactly one client (docs/adr/0003), and those two are what a
reader scanning a list is looking for.

Observed failing against the version without the ORDER BY (2026-09-19):
the join came back in insertion order -- zeta, alfa, client, project --
i.e. exactly the reverse of what a "+N" cap needs, so the two chips that
identify the row were the two it would have hidden.
"""

from __future__ import annotations

import uuid

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.tag import TagKind
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services import taxonomy
from mycelium_core.services.auth import signup
from mycelium_core.services.taxonomy import ClientInput


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="CHIPS",
        )
    return r.org_id, r.user_id


async def test_tag_chips_come_back_structural_first_then_alphabetical() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        client = await taxonomy.create_client(
            s,
            org_id=org,
            actor_id=user,
            name="Acme",
            profile=ClientInput(legal_name="Acme SRL"),
        )
        project = await taxonomy.create_project(
            s, org_id=org, actor_id=user, name="Acme-proj", client_tag_id=client.id
        )
        # Attached in the reverse of the expected order, and with the
        # generic pair anti-alphabetical, so insertion order cannot pass
        # for the contract by accident.
        zeta = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.generic, name="zeta"
        )
        alfa = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.generic, name="alfa"
        )
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="chips")
        for tag_id in (zeta.id, alfa.id, project.id):
            await tasks_svc.attach_tag(s, org_id=org, actor_id=user, task_id=task.id, tag_id=tag_id)
        task_id = task.id

    async with tenant_session(str(org), str(user)) as s:
        chips = (await tasks_svc.tags_by_task(s, task_ids=[task_id]))[task_id]

    assert [(t.kind, t.name) for t in chips] == [
        (TagKind.project, "Acme-proj"),
        (TagKind.client, "Acme"),
        (TagKind.generic, "alfa"),
        (TagKind.generic, "zeta"),
    ]
