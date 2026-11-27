"""One way to connect two things, notes or tasks (ADR-0077).

Every mechanism, through the one contract: connect, list from both ends
with the relation and the direction a person reads, disconnect. Plus what
the contract refuses: a relation no mechanism carries between those kinds,
a connection to itself, a mention or a promotion removed by unlinking, and
a task made a subtask of its own subtask.
"""

from __future__ import annotations

import uuid

import pytest

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.note import NoteKind
from mycelium_core.services import connections as conn
from mycelium_core.services import note_links
from mycelium_core.services import notes as nt
from mycelium_core.services import tasks as ts
from mycelium_core.services.auth import signup
from mycelium_core.services.connections import Direction, Item, ItemKind, Relation

N, T = ItemKind.note, ItemKind.task


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="CONN",
        )
    return r.org_id, r.user_id


async def _note(s: object, org: uuid.UUID, user: uuid.UUID, title: str, text: str = "x") -> Item:
    n = await nt.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title=title,
        text=text,
    )
    return Item(kind=N, id=n.id)


async def _task(s: object, org: uuid.UUID, user: uuid.UUID, title: str) -> Item:
    t = await ts.create_task(s, org_id=org, actor_id=user, title=title)  # type: ignore[arg-type]
    return Item(kind=T, id=t.id)


def _seen(rows: list[conn.Connection]) -> set[tuple[str, str, str | None]]:
    return {(c.relation.value, c.direction.value, c.other_title) for c in rows}


async def test_every_mechanism_round_trips_with_its_direction() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        origin = await _note(s, org, user, "origin")
        derived = await _note(s, org, user, "derived")
        old = await _note(s, org, user, "old")
        peer = await _note(s, org, user, "peer")
        work = await _task(s, org, user, "work")
        parent = await _task(s, org, user, "parent")
        first = await _task(s, org, user, "first")
        buddy = await _task(s, org, user, "buddy")

        async def link(a: Item, r: Relation, b: Item, **o: object) -> None:
            await conn.link(s, org_id=org, actor_id=user, source=a, relation=r, target=b, options=o)

        await link(derived, Relation.derives_from, origin)
        await link(derived, Relation.supersedes, old)
        await link(derived, Relation.related, peer)
        await link(work, Relation.works_on, derived)
        await link(derived, Relation.result_of, work)
        await link(work, Relation.subtask_of, parent)
        await link(work, Relation.depends_on, first, dependency_type="FS", lag_minutes=30)
        await link(work, Relation.related, buddy)

        seen = await conn.list_connections(s, org_id=org, item=derived)
        assert _seen(seen) == {
            ("derives_from", "out", "origin"),
            ("supersedes", "out", "old"),
            ("related", "none", "peer"),
            ("works_on", "in", "work"),
            ("result_of", "out", "work"),
        }
        assert _seen(await conn.list_connections(s, org_id=org, item=origin)) == {
            ("derives_from", "in", "derived")
        }
        assert _seen(await conn.list_connections(s, org_id=org, item=old)) == {
            ("supersedes", "in", "derived")
        }
        from_work = await conn.list_connections(s, org_id=org, item=work)
        assert _seen(from_work) == {
            ("works_on", "out", "derived"),
            ("result_of", "in", "derived"),
            ("subtask_of", "out", "parent"),
            ("depends_on", "out", "first"),
            ("related", "none", "buddy"),
        }
        dep = next(c for c in from_work if c.relation is Relation.depends_on)
        # "first" is still open, so the finish-to-start dependency holds.
        assert dep.detail == {"type": "FS", "lag_minutes": 30, "holding": True}
        assert _seen(await conn.list_connections(s, org_id=org, item=parent)) == {
            ("subtask_of", "in", "work")
        }
        assert _seen(await conn.list_connections(s, org_id=org, item=first)) == {
            ("depends_on", "in", "work")
        }

        # Each connection appears from both ends with the same handle; remove
        # each once.
        for handle in {c.handle for c in from_work + seen if c.removable}:
            await conn.unlink(s, org_id=org, actor_id=user, handle=handle)
        for item in (derived, origin, old, peer, work, parent, first, buddy):
            assert await conn.list_connections(s, org_id=org, item=item) == [], item


async def test_what_the_contract_refuses() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "a")
        t = await _task(s, org, user, "t")
        child = await _task(s, org, user, "child")

        async def refused(src: Item, r: Relation, dst: Item, code: MessageCode) -> None:
            with pytest.raises(DomainError) as e:
                await conn.link(s, org_id=org, actor_id=user, source=src, relation=r, target=dst)
            assert e.value.code == code

        await refused(a, Relation.subtask_of, t, MessageCode.CONNECTION_INVALID)
        await refused(a, Relation.related, a, MessageCode.CONNECTION_INVALID)
        await refused(a, Relation.mentions, t, MessageCode.CONNECTION_INVALID)
        await refused(t, Relation.supersedes, child, MessageCode.CONNECTION_INVALID)

        await conn.link(
            s, org_id=org, actor_id=user, source=child, relation=Relation.subtask_of, target=t
        )
        await refused(t, Relation.subtask_of, child, MessageCode.TASK_PARENT_CYCLE)
        await refused(t, Relation.subtask_of, t, MessageCode.CONNECTION_INVALID)

        # A promotion is listed, and cannot be removed by unlinking.
        task, _ = await note_links.promote_note_to_task(s, org_id=org, actor_id=user, note_id=a.id)
        promoted = [
            c
            for c in await conn.list_connections(s, org_id=org, item=a)
            if c.relation is Relation.promoted_to
        ]
        assert [(c.direction, c.removable) for c in promoted] == [(Direction.out, False)]
        with pytest.raises(DomainError):
            await conn.unlink(s, org_id=org, actor_id=user, handle=promoted[0].handle)
        assert task is not None


async def test_a_parent_cycle_is_refused_by_update_task_itself() -> None:
    """The defect at the origin: update_task accepted any parent."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await ts.create_task(s, org_id=org, actor_id=user, title="a")
        b = await ts.create_task(s, org_id=org, actor_id=user, title="b", parent_task_id=a.id)
        for target, parent in ((a, a.id), (a, b.id)):
            with pytest.raises(DomainError) as e:
                await ts.update_task(
                    s,
                    org_id=org,
                    actor_id=user,
                    task_id=target.id,
                    expected_version=target.version,
                    values={"parent_task_id": parent},
                )
            assert e.value.code == MessageCode.TASK_PARENT_CYCLE


async def test_a_mention_is_a_connection_from_both_ends() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        target = await _note(s, org, user, "target")
        source = await _note(s, org, user, "source", text=f"see [[{target.id}]]")
    async with tenant_session(str(org), str(user)) as s:
        out = await conn.list_connections(s, org_id=org, item=source)
        back = await conn.list_connections(s, org_id=org, item=target)
    assert ("mentions", "out", "target") in _seen(out)
    assert ("mentions", "in", "source") in _seen(back)
    assert all(not c.removable for c in out + back if c.relation is Relation.mentions)
