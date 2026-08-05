"""The two comment verbs the annotation family never had.

``comments:write`` could ADD a comment and DELETE one, but every path
that rewrites a body lived on ``annotations:write``, and no anchored
find/replace existed for an annotation body at any scope. Worse,
``soft_delete`` had no inverse anywhere in the tree: the row was
retained and unreachable, so "soft" delete was a one-way door in
practice.

These tests pin ``replace_in_body`` (the twin of
``note_parts.replace_in_part``, same no-op and concurrency contract) and
``restore`` (the inverse of ``soft_delete``, same author-or-admin gate).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import ConflictError, DomainError, ForbiddenError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.annotation import Annotation
from mycelium_core.services import annotations as anno
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup
from mycelium_core.services.memberships import add_member


async def _signup(org_name: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name=org_name,
        )
    return r.org_id, r.user_id


async def _task_comment(
    org: uuid.UUID, owner: uuid.UUID, body: str
) -> tuple[uuid.UUID, uuid.UUID, int]:
    async with tenant_session(str(org), str(owner)) as s:
        t = await tasks_svc.create_task(s, org_id=org, actor_id=owner, title="diary")
        c = await anno.create_comment(
            s,
            org_id=org,
            actor_id=owner,
            doc_kind="task_description",
            doc_id=t.id,
            body=body,
        )
        return t.id, c.id, c.version


async def _read(org: uuid.UUID, user: uuid.UUID, cid: uuid.UUID) -> tuple[str, int]:
    async with tenant_session(str(org), str(user)) as s:
        a = (await s.execute(select(Annotation).where(Annotation.id == cid))).scalar_one()
        return a.body or "", a.version


async def test_replace_swaps_one_passage_without_resending_the_body() -> None:
    org, owner = await _signup("anno-repl")
    _t, cid, ver = await _task_comment(org, owner, "ship on friday, review on friday")

    async with tenant_session(str(org), str(owner)) as s:
        new_ver, n = await anno.replace_in_body(
            s,
            org_id=org,
            actor_id=owner,
            annotation_id=cid,
            find="friday",
            replace="monday",
            expected_version=ver,
        )
    assert (new_ver, n) == (ver + 1, 2)
    assert await _read(org, owner, cid) == ("ship on monday, review on monday", ver + 1)


async def test_replace_count_limits_the_swap() -> None:
    org, owner = await _signup("anno-repl-n")
    _t, cid, ver = await _task_comment(org, owner, "a a a")
    async with tenant_session(str(org), str(owner)) as s:
        _v, n = await anno.replace_in_body(
            s,
            org_id=org,
            actor_id=owner,
            annotation_id=cid,
            find="a",
            replace="b",
            expected_version=ver,
            count=2,
        )
    assert n == 2
    assert (await _read(org, owner, cid))[0] == "b b a"


async def test_replace_noop_neither_bumps_the_version_nor_races() -> None:
    """A no-op changed nothing, so there is nothing to race: it must not
    bump the version and must not assert ``expected_version`` -- the same
    contract ``replace_in_part`` has, which callers rely on to make a
    blind replace idempotent."""
    org, owner = await _signup("anno-repl-noop")
    _t, cid, ver = await _task_comment(org, owner, "unchanged text")
    async with tenant_session(str(org), str(owner)) as s:
        new_ver, n = await anno.replace_in_body(
            s,
            org_id=org,
            actor_id=owner,
            annotation_id=cid,
            find="absent",
            replace="x",
            expected_version=ver + 99,  # deliberately stale: must be ignored
        )
    assert (new_ver, n) == (ver, 0)
    assert await _read(org, owner, cid) == ("unchanged text", ver)


async def test_replace_refuses_a_stale_version_when_it_does_change() -> None:
    org, owner = await _signup("anno-repl-stale")
    _t, cid, ver = await _task_comment(org, owner, "hello world")
    async with tenant_session(str(org), str(owner)) as s:
        await anno.edit(
            s,
            org_id=org,
            actor_id=owner,
            annotation_id=cid,
            body="hello world (edited)",
            expected_version=ver,
        )
    with pytest.raises(ConflictError):
        async with tenant_session(str(org), str(owner)) as s:
            await anno.replace_in_body(
                s,
                org_id=org,
                actor_id=owner,
                annotation_id=cid,
                find="hello",
                replace="goodbye",
                expected_version=ver,  # stale
            )


async def test_replace_refuses_to_outgrow_the_body_limit() -> None:
    org, owner = await _signup("anno-repl-cap")
    from mycelium_core.config import get_settings

    max_bytes = get_settings().note_body_max_bytes
    _t, cid, ver = await _task_comment(org, owner, "seed")
    with pytest.raises(DomainError) as err:
        async with tenant_session(str(org), str(owner)) as s:
            await anno.replace_in_body(
                s,
                org_id=org,
                actor_id=owner,
                annotation_id=cid,
                find="seed",
                replace="x" * (max_bytes + 1),
                expected_version=ver,
            )
    assert err.value.code is MessageCode.BODY_LIMIT_EXCEEDED


async def test_restore_undoes_a_soft_delete() -> None:
    """What makes ``delete_comment`` honestly soft. Before this the row
    was retained but unreachable on every surface."""
    org, owner = await _signup("anno-restore")
    _t, cid, ver = await _task_comment(org, owner, "work diary entry")

    async with tenant_session(str(org), str(owner)) as s:
        deleted_ver = await anno.soft_delete(
            s, org_id=org, actor_id=owner, annotation_id=cid, expected_version=ver
        )
    async with tenant_session(str(org), str(owner)) as s:
        # Unreachable while deleted.
        with pytest.raises(NotFoundError):
            await anno.get_annotation(s, org_id=org, annotation_id=cid)

    async with tenant_session(str(org), str(owner)) as s:
        restored_ver = await anno.restore(
            s, org_id=org, actor_id=owner, annotation_id=cid, expected_version=deleted_ver
        )
    assert restored_ver == deleted_ver + 1
    async with tenant_session(str(org), str(owner)) as s:
        back = await anno.get_annotation(s, org_id=org, annotation_id=cid)
        assert back.body == "work diary entry"
        assert back.deleted_at is None


async def test_restore_is_author_or_admin_like_the_delete_it_reverses() -> None:
    """A restore re-publishes someone else's words, so it cannot be a
    weaker gate than the delete."""
    org, owner = await _signup("anno-restore-authz")
    member_email = f"{uuid.uuid4().hex[:10]}@example.test"
    async with admin_session() as s:
        await signup(s, email=member_email, password="pw-strong-123", org_name="throwaway")
    async with tenant_session(str(org), str(owner)) as s:
        member_id = await add_member(
            s, org_id=org, actor_id=owner, email=member_email, role="member"
        )

    _t, cid, ver = await _task_comment(org, owner, "owner words")
    async with tenant_session(str(org), str(owner)) as s:
        deleted_ver = await anno.soft_delete(
            s, org_id=org, actor_id=owner, annotation_id=cid, expected_version=ver
        )

    with pytest.raises(ForbiddenError):
        async with tenant_session(str(org), str(member_id)) as s:
            await anno.restore(
                s,
                org_id=org,
                actor_id=member_id,
                annotation_id=cid,
                expected_version=deleted_ver,
            )


async def test_append_and_prepend_reach_a_comment_body() -> None:
    """The annotation family had no way to ADD to a body: every write
    resent the whole thing, while the note-part and task-description
    twins have had context-blind append/prepend for two tasks now."""
    org, owner = await _signup("anno-append")
    _t, cid, _ver = await _task_comment(org, owner, "middle")

    async with tenant_session(str(org), str(owner)) as s:
        v1, n1 = await anno.append_to_body(
            s, org_id=org, actor_id=owner, annotation_id=cid, text="tail"
        )
    assert n1 == len("tail")
    async with tenant_session(str(org), str(owner)) as s:
        v2, n2 = await anno.prepend_to_body(
            s, org_id=org, actor_id=owner, annotation_id=cid, text="head"
        )
    assert (n2, v2) == (len("head"), v1 + 1)
    assert (await _read(org, owner, cid))[0] == "head\n\nmiddle\n\ntail"


async def test_append_dedupe_makes_a_replay_a_noop() -> None:
    """Same retry-safety the task-description twin offers: a redelivered
    append must not double the paragraph."""
    org, owner = await _signup("anno-append-dedupe")
    _t, cid, _ver = await _task_comment(org, owner, "log")
    async with tenant_session(str(org), str(owner)) as s:
        await anno.append_to_body(s, org_id=org, actor_id=owner, annotation_id=cid, text="entry")
    async with tenant_session(str(org), str(owner)) as s:
        before = (await _read(org, owner, cid))[1]
        _v, n = await anno.append_to_body(
            s,
            org_id=org,
            actor_id=owner,
            annotation_id=cid,
            text="entry",
            dedupe_if_tail_matches=True,
        )
    assert n == 0
    assert (await _read(org, owner, cid)) == ("log\n\nentry", before)


async def test_append_refuses_to_outgrow_the_body_limit() -> None:
    org, owner = await _signup("anno-append-cap")
    from mycelium_core.config import get_settings

    max_bytes = get_settings().note_body_max_bytes
    _t, cid, _ver = await _task_comment(org, owner, "seed")
    with pytest.raises(DomainError) as err:
        async with tenant_session(str(org), str(owner)) as s:
            await anno.append_to_body(
                s,
                org_id=org,
                actor_id=owner,
                annotation_id=cid,
                text="x" * (max_bytes + 1),
            )
    assert err.value.code is MessageCode.BODY_LIMIT_EXCEEDED


async def test_get_annotation_can_read_a_deleted_row() -> None:
    """``restore`` needs the deleted comment's ``version``, and a caller
    that did not perform the delete has no other way to learn it."""
    org, owner = await _signup("anno-get-deleted")
    _t, cid, ver = await _task_comment(org, owner, "gone")
    async with tenant_session(str(org), str(owner)) as s:
        deleted_ver = await anno.soft_delete(
            s, org_id=org, actor_id=owner, annotation_id=cid, expected_version=ver
        )
    async with tenant_session(str(org), str(owner)) as s:
        with pytest.raises(NotFoundError):
            await anno.get_annotation(s, org_id=org, annotation_id=cid)
        found = await anno.get_annotation(s, org_id=org, annotation_id=cid, include_deleted=True)
        assert found.version == deleted_ver
        assert found.deleted_at is not None
