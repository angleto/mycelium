"""c87d46c4 — search() scoping is explicit + observable, and the task branch
gains date/assignee/state facets.

The footgun the audit found (#5): passing ``project_id`` scoped the note/blob
branches but NOT the task branch (org-wide), silently. Now every hit carries
``scope`` ('org' | 'project'); ``task_scope='project'`` opts the task branch
into project scoping; and ``due_before`` / ``assignee_handles`` / ``state_id``
narrow the task branch so 'tasks due today assigned to X' is answerable via
search too.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from _fake_embedder import FakeEmbedder

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.models.tag import TagKind
from mycelium_core.services import billing
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services import taxonomy as taxonomy_svc
from mycelium_core.services.auth import signup
from mycelium_mcp.server import search


@pytest.fixture
def _fake_embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


async def _seed() -> tuple[str, str, uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="scope",
        )
    assert r.token is not None
    # Grant + rate-card the fake embedder so the metered resync embed runs the
    # dense path deterministically (keyword FTS would find the tokens anyway).
    async with tenant_session(str(r.org_id), str(r.user_id)) as s:
        await billing.grant_credits(s, org_id=r.org_id, actor_id=r.user_id, amount=Decimal(100))
        await billing.upsert_rate_card(
            s,
            org_id=r.org_id,
            actor_id=r.user_id,
            model_id=FakeEmbedder.model_id,
            provider="local",
            values={"credits_per_input": Decimal("0.001")},
        )
    return r.token, str(r.org_id), r.org_id, r.user_id


async def test_task_branch_scope_label_and_task_scope(_fake_embedder: None) -> None:
    token, org, org_id, user_id = await _seed()
    async with tenant_session(org, str(user_id)) as s:
        proj = await taxonomy_svc.create_tag(
            s, org_id=org_id, actor_id=user_id, kind=TagKind.generic, name="projA"
        )
        await tasks_svc.create_task(
            s,
            org_id=org_id,
            actor_id=user_id,
            title="zephyrtoken inside project",
            tag_ids=[proj.id],
        )
        await tasks_svc.create_task(
            s, org_id=org_id, actor_id=user_id, title="zephyrtoken outside project"
        )
        proj_id = str(proj.id)

    # Default: the task branch is org-wide even WITH project_id set -> both
    # hits, each labelled scope='org' so the caller is never misled. search()
    # now returns the {hits, meta} envelope (task 4f3c2207).
    envelope = await search(
        token=token, org_id=org, q="zephyrtoken", kinds=["task"], project_id=proj_id
    )
    assert "meta" in envelope
    res = envelope["hits"]
    titles = {h["title"] for h in res}
    assert any("inside" in t for t in titles) and any("outside" in t for t in titles)
    assert res and all(h["scope"] == "org" for h in res)
    assert all("scope" in h for h in res)

    # task_scope='project' ANDs the project tag into the task branch -> only
    # the project-tagged task, now labelled scope='project'.
    scoped = (
        await search(
            token=token,
            org_id=org,
            q="zephyrtoken",
            kinds=["task"],
            project_id=proj_id,
            task_scope="project",
        )
    )["hits"]
    stitles = {h["title"] for h in scoped}
    assert any("inside" in t for t in stitles)
    assert not any("outside" in t for t in stitles)
    assert scoped and all(h["scope"] == "project" for h in scoped)


async def test_task_branch_facets(_fake_embedder: None) -> None:
    token, org, org_id, user_id = await _seed()
    today = dt.date.today()
    async with tenant_session(org, str(user_id)) as s:
        await tasks_svc.create_task(
            s,
            org_id=org_id,
            actor_id=user_id,
            title="quokkatoken due soon",
            due_date=today + dt.timedelta(days=2),
        )
        await tasks_svc.create_task(
            s,
            org_id=org_id,
            actor_id=user_id,
            title="quokkatoken due far",
            due_date=today + dt.timedelta(days=60),
        )

    # No facet: both found.
    both = (await search(token=token, org_id=org, q="quokkatoken", kinds=["task"]))["hits"]
    assert len({h["title"] for h in both}) == 2

    # due_before facet (half-open upper bound): only the soon task.
    cutoff = (today + dt.timedelta(days=3)).isoformat()
    soon = (
        await search(token=token, org_id=org, q="quokkatoken", kinds=["task"], due_before=cutoff)
    )["hits"]
    stitles = {h["title"] for h in soon}
    assert any("soon" in t for t in stitles) and not any("far" in t for t in stitles)

    # A non-matching state filters every task hit out (the state facet runs).
    none_state = await search(
        token=token, org_id=org, q="quokkatoken", kinds=["task"], state_id=str(uuid.uuid4())
    )
    assert none_state["hits"] == []

    # A bogus assignee handle likewise filters every task hit out.
    none_assignee = await search(
        token=token, org_id=org, q="quokkatoken", kinds=["task"], assignee_handles=["ghost-nobody"]
    )
    assert none_assignee["hits"] == []
