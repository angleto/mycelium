"""``whoami`` bootstrap tool (portable memory: an LLM / MCP client resumes from
Mycelium at session start instead of a machine-local file). Read-only; resolves
identity + granted scope, open assigned tasks, and a recall of the agent memory
lane (channel 'agent')."""

from __future__ import annotations

import secrets
import uuid

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.agent_token import AgentToken
from mycelium_core.models.ai_assistant import AiAssistant
from mycelium_core.services import identities, tasks
from mycelium_core.services.auth import signup
from mycelium_mcp.server import _PRINCIPAL, whoami


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_whoami_assistant_token_resolves_identity_scope_tasks() -> None:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="WHO")
    org, user = r.org_id, r.user_id
    handle = f"claude-{uuid.uuid4().hex[:8]}"
    async with tenant_session(str(org), str(user)) as s:
        assistant = AiAssistant(
            org_id=org,
            user_id=user,
            label="Claude",
            handle=handle,
            scope=["tasks:read", "notes:read"],
            is_active=True,
        )
        s.add(assistant)
        await s.flush()
        await identities.ensure_for_ai_assistant(s, org_id=org, assistant_id=assistant.id)
        tok = AgentToken(
            org_id=org,
            user_id=user,
            name="t",
            prefix=f"mycelium_at_{secrets.token_hex(4)}",
            token_hash=secrets.token_bytes(32),
            scope="mcp",
            assistant_id=assistant.id,
        )
        s.add(tok)
        await s.flush()
        token_id = tok.id
        task = await tasks.create_task(
            s, org_id=org, actor_id=user, title="do the thing", assignee_handle=handle
        )
        task_id = str(task.id)

    # Simulate the HTTP transport: the bearer middleware publishes the principal.
    reset = _PRINCIPAL.set((user, org, token_id))
    try:
        me = await whoami("", "")
    finally:
        _PRINCIPAL.reset(reset)

    assert me["org_id"] == str(org)
    assert me["identity"]["handle"] == handle
    assert me["identity"]["kind"] == "ai_assistant"
    assert me["identity"]["assistant_label"] == "Claude"
    # Granted scope is surfaced verbatim (this closes the "captured-but-unreadable"
    # gap for the whoami surface): null would mean full access.
    assert me["scope"] == ["tasks:read", "notes:read"]
    # The open task assigned to the assistant is surfaced.
    assert task_id in [t["id"] for t in me["open_tasks"]]
    # The durable memory lane is advertised (recall may be empty if the channel
    # is unseeded / no embedder -- a bootstrap must never fail on that).
    assert me["memory_lane"]["channel"] == "agent"
    assert isinstance(me["memory_lane"]["recall"], list)
    assert "protocol" in me["pointers"]


async def test_whoami_plain_principal_is_safe() -> None:
    # stdio / human-bearer path (token_id None): still returns a valid shape,
    # scope is None (full access), and never raises.
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="WHO2")
    org, user = r.org_id, r.user_id
    reset = _PRINCIPAL.set((user, org, None))
    try:
        me = await whoami("", "")
    finally:
        _PRINCIPAL.reset(reset)
    assert me["org_id"] == str(org)
    assert me["scope"] is None
    assert me["memory_lane"]["channel"] == "agent"
    assert isinstance(me["open_tasks"], list)
    # The hint carries the shape of an entry, not only the call (task 9f4a2fd9):
    # a fact says where and when it was verified, a lesson is cue / lesson /
    # evidence / confidence, no observed contrast means no entry, and a recalled
    # fact from elsewhere is a hypothesis. Observed failing against 59bc556b,
    # whose hint was the bare call signature.
    hint = me["memory_lane"]["write_hint"]
    for part in ("FACT", "cue:", "lesson:", "evidence:", "confidence:", "no entry", "hypothesis"):
        assert part in hint, part


async def test_whoami_recall_spans_every_project_and_labels_each_entry() -> None:
    """The recall reaches the lane in every project and says where each entry
    comes from (task a21157fc).

    Observed failing against 59bc556b: there the recall asked for the perimeter
    of blobs with NO project, so of the three entries below it returned only
    the untagged one, never the two filed under a project -- the shape that, in
    production on 2026-10-22, handed every session the same single blob and
    never the lane's index."""
    from _fake_embedder import FakeEmbedder

    from mycelium_core.embedder import set_embedder_override
    from mycelium_core.services import memory, taxonomy

    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="WHO3")
    org, user = r.org_id, r.user_id
    set_embedder_override(FakeEmbedder)
    try:
        async with tenant_session(str(org), str(user)) as s:
            alpha = await taxonomy.create_project(s, org_id=org, actor_id=user, name="Alpha")
            beta = await taxonomy.create_project(s, org_id=org, actor_id=user, name="Beta")
            for project_id, tag_ids, text in (
                (alpha.id, [], "agent memory index protocol operational state alpha"),
                (beta.id, [], "agent memory index protocol operational state beta"),
                # A NULL perimeter riding a project TAG, the way task blobs are
                # filed by design: labelled by its tag, not as projectless.
                (None, [beta.id], "agent memory index protocol operational state tagged"),
                # Two project tags: both named, not whichever came first.
                (None, [beta.id, alpha.id], "agent memory index protocol operational state both"),
            ):
                await memory.write_blob(
                    s,
                    org_id=org,
                    actor_id=user,
                    project_id=project_id,
                    text_body=text,
                    operation_id=f"w-{uuid.uuid4().hex}",
                    namespace="agent",
                    channel_key="agent",
                    tag_ids=tag_ids,
                )
        reset = _PRINCIPAL.set((user, org, None))
        try:
            me = await whoami("", "")
        finally:
            _PRINCIPAL.reset(reset)
    finally:
        set_embedder_override(None)

    by_text = {e["blob"]["text"].rsplit(" ", 1)[-1]: e for e in me["memory_lane"]["recall"]}
    assert set(by_text) == {"alpha", "beta", "tagged", "both"}
    assert by_text["alpha"]["from_projects"] == ["Alpha"]
    assert by_text["beta"]["from_projects"] == ["Beta"]
    assert by_text["tagged"]["from_projects"] == ["Beta"]
    assert by_text["both"]["from_projects"] == ["Alpha", "Beta"]
    assert all(len(e["written_on"]) == 10 for e in by_text.values())
    assert "hypothesis" in me["memory_lane"]["recall_scope"]


def test_a_perimeter_whose_project_is_gone_is_named_not_dropped() -> None:
    """An empty list means "no project"; an entry whose perimeter points at a
    project tag that no longer resolves has one, so it is named by its id."""
    from mycelium_core.models.memory_blob import MemoryBlob
    from mycelium_mcp.server import _recall_projects

    gone = uuid.uuid4()
    blob = MemoryBlob(project_id=gone)
    assert _recall_projects(blob, None, {}) == [f"unknown project {str(gone)[:8]}"]
    assert _recall_projects(MemoryBlob(project_id=None), None, {}) == []
