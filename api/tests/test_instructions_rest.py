"""Instructions over REST, with a real agent token (task 0d323273).

The fence on an instruction keys on the CREDENTIAL, like protected notes,
because the REST adapter opens every bearer as ``human_api``: a guard written
on the actor kind would hold on MCP and leave this surface open. So the test
that decides whether the fence is real is the HTTP one, with a token bound to
an assistant, next to the same call made with the person's own bearer.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
from tests_helpers import seed_ai_assistant_identity

from mycelium_api.main import app
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.services import agent_tokens as at_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup

_RESERVED = "annotation.instruction_reserved"


def _h(token: str, org: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Workspace-Id": str(org)}


async def _setup() -> tuple[uuid.UUID, str, str, str, uuid.UUID, uuid.UUID]:
    """An org, the person's bearer, an assistant's bound bearer and handle,
    a task whose description the instruction is about, and the person's id."""
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="INSTR",
        )
    assert r.token is not None
    async with tenant_session(str(r.org_id), str(r.user_id)) as s:
        ident = await seed_ai_assistant_identity(
            s, org_id=r.org_id, user_id=r.user_id, label="claude"
        )
        minted = await at_svc.mint(
            s, org_id=r.org_id, actor_id=r.user_id, name="bound", assistant_id=ident.ai_assistant_id
        )
        task = await tasks_svc.create_task(
            s, org_id=r.org_id, actor_id=r.user_id, title="t", description="ship on friday"
        )
    return r.org_id, r.token, minted.raw, ident.handle, task.id, r.user_id


async def test_an_instruction_over_rest_is_answered_by_the_agent_and_closed_by_the_person() -> None:
    org, person, agent, agent_handle, task_id, _user = await _setup()
    doc = {"doc_kind": "task_description", "doc_id": str(task_id)}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/annotations/comment",
            headers=_h(person, org),
            json={
                **doc,
                "body": "make the day concrete",
                "anchor_quote": "friday",
                "assignee_handle": agent_handle,
            },
        )
        assert r.status_code == 200, r.text
        ins = r.json()
        assert (ins["assignee_kind"], ins["assignee_handle"]) == ("ai_assistant", agent_handle)
        assert ins["author_kind"] == "user"

        r = await c.post(
            "/annotations/suggestion",
            headers=_h(agent, org),
            json={
                **doc,
                "original_text": "friday",
                "proposed_text": "friday 30 October",
                "instruction_id": ins["id"],
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["instruction_id"] == ins["id"]

        # The agent finds it in its own inbox, with no handle passed: the
        # calling identity is the assistant, not the token's owner.
        inbox = (await c.get("/annotations/assigned", headers=_h(agent, org))).json()
        assert [a["id"] for a in inbox] == [ins["id"]]
        mine = (await c.get("/annotations/assigned", headers=_h(person, org))).json()
        assert mine == []

        # The agent cannot close or reassign the instruction it was given...
        r = await c.post(
            f"/annotations/{ins['id']}/resolve",
            headers=_h(agent, org),
            json={"expected_version": ins["version"]},
        )
        assert r.status_code == 403, r.text
        assert r.json()["code"] == _RESERVED
        r = await c.post(
            f"/annotations/{ins['id']}/assign",
            headers=_h(agent, org),
            json={"expected_version": ins["version"], "clear": True},
        )
        assert r.status_code == 403, r.text
        assert r.json()["code"] == _RESERVED

        # ...but it can answer on it, and the person closes it.
        r = await c.post(
            "/annotations/comment",
            headers=_h(agent, org),
            json={**doc, "body": "done in one suggestion", "parent_id": ins["id"]},
        )
        assert r.status_code == 200, r.text
        r = await c.post(
            f"/annotations/{ins['id']}/resolve",
            headers=_h(person, org),
            json={"expected_version": ins["version"]},
        )
        assert r.status_code == 200, r.text

        rows = (await c.get("/annotations", headers=_h(person, org), params=doc)).json()
        by_id = {a["id"]: a for a in rows}
        assert by_id[ins["id"]]["status"] == "resolved"
        answers = [a for a in rows if a["instruction_id"] == ins["id"]]
        assert [a["kind"] for a in answers] == ["suggestion"]


async def test_the_cli_token_cannot_close_an_instruction() -> None:
    """The CLI's token is bound to no assistant, and the fence asked only
    about bound tokens, so the CLI, or any agent on the machine reading its
    credential file, could close the instruction its own work is checked
    against. It is an agent's credential and is refused like one."""
    org, person, _agent, agent_handle, task_id, user = await _setup()
    async with tenant_session(str(org), str(user)) as s:
        cli = (await at_svc.mint(s, org_id=org, actor_id=user, name="cli", scope="cli")).raw
    doc = {"doc_kind": "task_description", "doc_id": str(task_id)}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        ins = (
            await c.post(
                "/annotations/comment",
                headers=_h(person, org),
                json={
                    **doc,
                    "body": "make the day concrete",
                    "anchor_quote": "friday",
                    "assignee_handle": agent_handle,
                },
            )
        ).json()
        r = await c.post(
            f"/annotations/{ins['id']}/resolve",
            headers=_h(cli, org),
            json={"expected_version": ins["version"]},
        )
        assert r.status_code == 403, r.text
        assert r.json()["code"] == _RESERVED
