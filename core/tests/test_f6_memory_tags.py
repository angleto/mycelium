"""F6 memory tags (DB-backed): tags are a facet inside the hard
(org, project) boundary. Covers explicit tags on write, inheritance
from tagged provenance, cross-org rejection, the faceted-AND retrieve
filter, the consolidation union, and post-hoc attach/detach.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from _fake_embedder import FakeEmbedder

from flow_core.db import admin_session, tenant_session
from flow_core.models.tag import TagKind
from flow_core.services import billing, taxonomy
from flow_core.services import memory as mem
from flow_core.services import tasks as tasks_svc
from flow_core.services.auth import signup

_FAKE = FakeEmbedder()


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org(name: str = "MEMTAG") -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name=name)
    return r.org_id, r.user_id


async def _seed_billing(s, org: uuid.UUID, user: uuid.UUID) -> None:
    await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(1000))
    await billing.upsert_rate_card(
        s,
        org_id=org,
        actor_id=user,
        model_id=FakeEmbedder.model_id,
        provider="local",
        values={"credits_per_input": Decimal("0.001")},
    )


async def test_explicit_and_inherited_tags_cross_org_rejected() -> None:
    org, user = await _org()
    other_org, other_user = await _org("OTHER")
    async with tenant_session(str(other_org), str(other_user)) as s:
        foreign = await taxonomy.create_tag(
            s, org_id=other_org, actor_id=other_user, kind=TagKind.generic, name="foreign"
        )
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        topic = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.generic, name="topic"
        )
        # A task carrying a tag, used as provenance for inheritance.
        task = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="src task"
        )
        src_tag = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.generic, name="from-task"
        )
        await tasks_svc.attach_tag(
            s, org_id=org, actor_id=user, task_id=task.id, tag_id=src_tag.id
        )
        blob = await mem.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            text_body="derived note",
            operation_id="w1",
            sources=[("task", str(task.id))],
            tag_ids=[topic.id, foreign.id],  # foreign must be dropped
            embedder=_FAKE,
        )
        tagmap = await mem.tags_by_blob(s, blob_ids=[blob.id])
    got = {t.id for t in tagmap[blob.id]}
    assert got == {topic.id, src_tag.id}  # explicit (own) + inherited
    assert foreign.id not in got  # cross-org tag rejected


async def test_retrieve_tag_filter_is_faceted_and() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        a = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.generic, name="a"
        )
        b = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.generic, name="b"
        )
        only_a = await mem.write_blob(
            s, org_id=org, actor_id=user, project_id=None,
            text_body="report one", operation_id="wa",
            tag_ids=[a.id], embedder=_FAKE,
        )
        a_and_b = await mem.write_blob(
            s, org_id=org, actor_id=user, project_id=None,
            text_body="report two", operation_id="wb",
            tag_ids=[a.id, b.id], embedder=_FAKE,
        )

        both = await mem.retrieve(
            s, org_id=org, actor_id=user, project_id=None,
            query="report", operation_id="q1", tag_ids=[a.id], embedder=_FAKE,
        )
        assert {h.blob.id for h in both} == {only_a.id, a_and_b.id}

        strict = await mem.retrieve(
            s, org_id=org, actor_id=user, project_id=None,
            query="report", operation_id="q2",
            tag_ids=[a.id, b.id], embedder=_FAKE,
        )
        assert {h.blob.id for h in strict} == {a_and_b.id}


async def test_consolidate_unions_member_tags() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        a = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.generic, name="a"
        )
        b = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.generic, name="b"
        )
        m1 = await mem.write_blob(
            s, org_id=org, actor_id=user, project_id=None,
            text_body="alpha", operation_id="m1", tag_ids=[a.id], embedder=_FAKE,
        )
        m2 = await mem.write_blob(
            s, org_id=org, actor_id=user, project_id=None,
            text_body="beta", operation_id="m2", tag_ids=[b.id], embedder=_FAKE,
        )
        concept = await mem.consolidate(
            s, org_id=org, actor_id=user, project_id=None,
            blob_ids=[m1.id, m2.id], operation_id="c1", embedder=_FAKE,
        )
        tagmap = await mem.tags_by_blob(s, blob_ids=[concept.id])
    assert {t.id for t in tagmap[concept.id]} == {a.id, b.id}


async def test_attach_detach_blob_tag_idempotent() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        tag = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.generic, name="curated"
        )
        blob = await mem.write_blob(
            s, org_id=org, actor_id=user, project_id=None,
            text_body="curate me", operation_id="w1", embedder=_FAKE,
        )
        await mem.attach_blob_tag(
            s, org_id=org, actor_id=user, blob_id=blob.id, tag_id=tag.id
        )
        # Idempotent: re-attaching is a no-op, not a PK violation.
        await mem.attach_blob_tag(
            s, org_id=org, actor_id=user, blob_id=blob.id, tag_id=tag.id
        )
        tagmap = await mem.tags_by_blob(s, blob_ids=[blob.id])
        assert {t.id for t in tagmap[blob.id]} == {tag.id}

        await mem.detach_blob_tag(
            s, org_id=org, actor_id=user, blob_id=blob.id, tag_id=tag.id
        )
        tagmap = await mem.tags_by_blob(s, blob_ids=[blob.id])
    assert blob.id not in tagmap
