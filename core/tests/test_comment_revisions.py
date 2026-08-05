"""Recovery history for comments (migration 0090) + the purge.

Tasks and notes have had a revision timeline since migration 0006.
Comments -- the third markdown document in the model, addressed through
the same ``doc_kind`` handle -- had none: an edit bumped ``version`` and
that was the entire record. The old words were gone, and the timeline
that shows "who changed what, when" for every other body simply had
nothing to say about a work-diary entry.

Pinned here: every mutation leaves a tagged row; only the BODY is
restorable (a restore must not un-resolve a thread or re-assign it); and
the purge takes the whole history with it, which is the difference
between it and the ordinary soft delete.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import DomainError, ForbiddenError, NotFoundError
from mycelium_core.models.annotation import Annotation
from mycelium_core.models.entity_revision import EntityRevision
from mycelium_core.services import annotations as anno
from mycelium_core.services import entity_revisions as revs
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


async def _comment(org: uuid.UUID, owner: uuid.UUID, body: str) -> tuple[uuid.UUID, int]:
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
        return c.id, c.version


async def _revisions(org: uuid.UUID, user: uuid.UUID, cid: uuid.UUID) -> list[EntityRevision]:
    async with tenant_session(str(org), str(user)) as s:
        return await revs.list_revisions(
            s, entity_kind=revs.ENTITY_KIND_ANNOTATION, entity_id=cid, limit=50
        )


async def _body(org: uuid.UUID, user: uuid.UUID, cid: uuid.UUID) -> tuple[str, int]:
    async with tenant_session(str(org), str(user)) as s:
        a = (await s.execute(select(Annotation).where(Annotation.id == cid))).scalar_one()
        return a.body or "", a.version


async def test_creating_a_comment_opens_its_timeline() -> None:
    org, owner = await _signup("crev-create")
    cid, _v = await _comment(org, owner, "first words")
    rows = await _revisions(org, owner, cid)
    assert len(rows) == 1
    assert list(rows[0].changed_fields) == ["_create"]
    assert rows[0].snapshot["body"] == "first words"
    assert rows[0].entity_kind == "annotation"


async def test_the_old_words_survive_an_edit() -> None:
    """The whole point. Before this the previous body was simply gone."""
    org, owner = await _signup("crev-edit")
    cid, v = await _comment(org, owner, "original wording")
    async with tenant_session(str(org), str(owner)) as s:
        await anno.edit(
            s,
            org_id=org,
            actor_id=owner,
            annotation_id=cid,
            body="revised wording",
            expected_version=v,
        )
    rows = await _revisions(org, owner, cid)
    bodies = [r.snapshot["body"] for r in rows]
    assert "revised wording" in bodies
    assert "original wording" in bodies


async def test_every_body_verb_funnels_into_one_revision_each() -> None:
    """replace / append / prepend all persist through ``edit``, so each
    lands exactly one ``body`` row rather than none or two."""
    org, owner = await _signup("crev-verbs")
    cid, v = await _comment(org, owner, "alpha")
    async with tenant_session(str(org), str(owner)) as s:
        await anno.replace_in_body(
            s,
            org_id=org,
            actor_id=owner,
            annotation_id=cid,
            find="alpha",
            replace="beta",
            expected_version=v,
        )
    async with tenant_session(str(org), str(owner)) as s:
        await anno.append_to_body(s, org_id=org, actor_id=owner, annotation_id=cid, text="tail")
    async with tenant_session(str(org), str(owner)) as s:
        await anno.prepend_to_body(s, org_id=org, actor_id=owner, annotation_id=cid, text="head")
    rows = await _revisions(org, owner, cid)
    body_rows = [r for r in rows if list(r.changed_fields) == ["body"]]
    assert len(body_rows) == 3


async def test_lifecycle_transitions_are_on_the_timeline() -> None:
    org, owner = await _signup("crev-lifecycle")
    cid, v = await _comment(org, owner, "entry")
    async with tenant_session(str(org), str(owner)) as s:
        v = await anno.resolve(s, org_id=org, actor_id=owner, annotation_id=cid, expected_version=v)
    async with tenant_session(str(org), str(owner)) as s:
        v = await anno.reopen(s, org_id=org, actor_id=owner, annotation_id=cid, expected_version=v)
    async with tenant_session(str(org), str(owner)) as s:
        v = await anno.soft_delete(
            s, org_id=org, actor_id=owner, annotation_id=cid, expected_version=v
        )
    async with tenant_session(str(org), str(owner)) as s:
        await anno.restore(s, org_id=org, actor_id=owner, annotation_id=cid, expected_version=v)
    tags = {tok for r in await _revisions(org, owner, cid) for tok in (r.changed_fields or [])}
    assert {"_create", "status", "_delete", "_restore"} <= tags


async def test_restore_revision_reverts_the_body() -> None:
    org, owner = await _signup("crev-restore")
    cid, v = await _comment(org, owner, "the good version")
    baseline = (await _revisions(org, owner, cid))[0].id
    async with tenant_session(str(org), str(owner)) as s:
        v2 = await anno.edit(
            s,
            org_id=org,
            actor_id=owner,
            annotation_id=cid,
            body="the regrettable version",
            expected_version=v,
        )
    async with tenant_session(str(org), str(owner)) as s:
        v3 = await anno.restore_revision(
            s,
            org_id=org,
            actor_id=owner,
            annotation_id=cid,
            revision_id=baseline,
            expected_version=v2,
        )
    body, version = await _body(org, owner, cid)
    assert body == "the good version"
    assert version == v3
    # The restore is itself a revision, on the restore channel, pointing
    # at what it replayed -- so the timeline stays monotonic and the
    # restore can itself be undone.
    latest = (await _revisions(org, owner, cid))[0]
    assert latest.channel == "restore"
    assert latest.restored_from == baseline


async def test_restore_reverts_the_words_not_the_routing() -> None:
    """``body`` is the only restorable field: replaying an old snapshot
    must not un-resolve a thread that was since resolved."""
    org, owner = await _signup("crev-restore-scope")
    cid, v = await _comment(org, owner, "open text")
    baseline = (await _revisions(org, owner, cid))[0].id
    async with tenant_session(str(org), str(owner)) as s:
        v = await anno.edit(
            s,
            org_id=org,
            actor_id=owner,
            annotation_id=cid,
            body="edited text",
            expected_version=v,
        )
    async with tenant_session(str(org), str(owner)) as s:
        v = await anno.resolve(s, org_id=org, actor_id=owner, annotation_id=cid, expected_version=v)
    async with tenant_session(str(org), str(owner)) as s:
        await anno.restore_revision(
            s,
            org_id=org,
            actor_id=owner,
            annotation_id=cid,
            revision_id=baseline,
            expected_version=v,
        )
    async with tenant_session(str(org), str(owner)) as s:
        back = await anno.get_annotation(s, org_id=org, annotation_id=cid)
        assert back.body == "open text"
        assert back.status == "resolved", "a body restore must not touch the thread status"


async def test_purge_destroys_the_comment_and_its_whole_history() -> None:
    """The difference between the purge and the soft delete: the soft
    delete keeps every snapshot (that is what makes it restorable), the
    purge leaves nothing -- via ``trg_comment_revision_cascade``, since
    no foreign key reaches a polymorphic revision row."""
    org, owner = await _signup("crev-purge")
    cid, v = await _comment(org, owner, "sensitive wording")
    async with tenant_session(str(org), str(owner)) as s:
        await anno.edit(
            s,
            org_id=org,
            actor_id=owner,
            annotation_id=cid,
            body="still sensitive",
            expected_version=v,
        )
    assert len(await _revisions(org, owner, cid)) >= 2

    async with tenant_session(str(org), str(owner)) as s:
        await anno.purge(s, org_id=org, actor_id=owner, annotation_id=cid)

    async with tenant_session(str(org), str(owner)) as s:
        with pytest.raises(NotFoundError):
            await anno.get_annotation(s, org_id=org, annotation_id=cid, include_deleted=True)
    assert await _revisions(org, owner, cid) == []


async def test_purge_reaches_an_already_deleted_comment() -> None:
    org, owner = await _signup("crev-purge-deleted")
    cid, v = await _comment(org, owner, "withdrawn")
    async with tenant_session(str(org), str(owner)) as s:
        await anno.soft_delete(s, org_id=org, actor_id=owner, annotation_id=cid, expected_version=v)
    async with tenant_session(str(org), str(owner)) as s:
        await anno.purge(s, org_id=org, actor_id=owner, annotation_id=cid)
    assert await _revisions(org, owner, cid) == []


async def test_purge_is_admin_only_not_author_or_admin() -> None:
    """Every other comment write lets the AUTHOR act on their own words,
    which is right for something reversible. Erasing a signed entry from
    a shared conversation so no trace remains is a different act, and the
    author is exactly who is most likely to want it."""
    org, owner = await _signup("crev-purge-authz")
    member_email = f"{uuid.uuid4().hex[:10]}@example.test"
    async with admin_session() as s:
        await signup(s, email=member_email, password="pw-strong-123", org_name="throwaway")
    async with tenant_session(str(org), str(owner)) as s:
        member_id = await add_member(
            s, org_id=org, actor_id=owner, email=member_email, role="member"
        )
    # A comment authored BY the member: they may edit and delete it...
    async with tenant_session(str(org), str(member_id)) as s:
        t = await tasks_svc.create_task(s, org_id=org, actor_id=owner, title="shared")
        c = await anno.create_comment(
            s,
            org_id=org,
            actor_id=member_id,
            doc_kind="task_description",
            doc_id=t.id,
            body="my own words",
        )
        cid = c.id
    # ...but not purge it.
    with pytest.raises(ForbiddenError):
        async with tenant_session(str(org), str(member_id)) as s:
            await anno.purge(s, org_id=org, actor_id=member_id, annotation_id=cid)
    async with tenant_session(str(org), str(owner)) as s:
        await anno.purge(s, org_id=org, actor_id=owner, annotation_id=cid)


async def test_a_revision_of_another_comment_is_not_restorable_here() -> None:
    """The (entity_kind, entity_id) pair is the guard: a revision id from
    a different comment -- or from a note -- must not cross over."""
    org, owner = await _signup("crev-cross")
    a_id, a_v = await _comment(org, owner, "comment A")
    b_id, _b_v = await _comment(org, owner, "comment B")
    b_rev = (await _revisions(org, owner, b_id))[0].id
    with pytest.raises((NotFoundError, DomainError)):
        async with tenant_session(str(org), str(owner)) as s:
            await anno.restore_revision(
                s,
                org_id=org,
                actor_id=owner,
                annotation_id=a_id,
                revision_id=b_rev,
                expected_version=a_v,
            )
