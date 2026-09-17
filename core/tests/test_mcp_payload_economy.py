"""What this surface refuses to spend tokens on.

An MCP result is not read once: it stays in the caller's context for the rest
of the session, so a field nobody reads is charged again on every later turn.
Measured over 32 recorded Claude Code sessions on 2026-09-17 (721 gateway
calls, 553,547 result tokens), four tools carried 77% of this server's context
weight and most of what they carried was not read: ranking floats, a model id
repeated per row, ids that were never passed back, and descriptions shipped
whole to callers that went straight to the shell afterwards.

These tests pin the decisions that came out of that measurement. They are
shape assertions, not size ones: a size gate lives in
``test_mcp_response_budget.py``.

Two surfaces appear here and the difference is load-bearing. The REGISTRY
(``mycelium_mcp.server``, imported directly) is what the stdio entrypoint and
most of this suite call; the GATEWAY (``execute_tool``) is what an MCP client
reaches over HTTP. Payload shape is decided in the registry's serializers and
is common to both; the short-id convention belongs to the gateway alone,
because the gateway is the only surface that can also accept a short id back.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from mycelium_core.db import admin_session
from mycelium_core.services.auth import signup
from mycelium_mcp.gateway import execute_tool
from mycelium_mcp.server import (
    _PRINCIPAL,
    _TASK_DESCRIPTION_CHARS,
    create_task,
    get_task,
    list_tasks,
    update_task,
)

#: An org to work in: its PAT (for registry calls) and the principal the
#: bearer middleware would publish (for gateway calls).
_Org = tuple[str, str, uuid.UUID]


async def _org() -> _Org:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="ECON",
        )
    assert r.token is not None
    return r.token, str(r.org_id), r.user_id


@asynccontextmanager
async def _as_principal(org: _Org) -> AsyncIterator[None]:
    """Run inside the gateway's authenticated-principal context, which is what
    the bearer middleware publishes in production."""
    _token, org_id, user_id = org
    reset = _PRINCIPAL.set((user_id, uuid.UUID(org_id), None))
    try:
        yield
    finally:
        _PRINCIPAL.reset(reset)


async def _task_with_description(token: str, org_id: str, body: str) -> str:
    created = await create_task(token=token, org_id=org_id, title="long one")
    await update_task(
        token=token,
        org_id=org_id,
        task_id=created["id"],
        expected_version=created["version"],
        description=body,
    )
    return str(created["id"])


# --- the description cap -----------------------------------------------------


async def test_get_task_caps_a_long_description_and_says_so() -> None:
    """A cut that does not announce itself is worse than an absent field: the
    caller cannot tell a description that ends there from one that was cut,
    and will act on the cut as if it were the whole. Same contract as
    ``_blob``'s ``text_truncated`` / ``text_chars``.

    Observed failing on 2026-09-17 against the uncapped serializer (``get_task``
    passing ``snippet_chars=None``): ``assert 1700 == 1200``."""
    token, org_id, _uid = await _org()
    body = "x" * (_TASK_DESCRIPTION_CHARS + 500)
    tid = await _task_with_description(token, org_id, body)

    got = await get_task(token=token, org_id=org_id, task_id=tid)
    assert len(got["description"]) == _TASK_DESCRIPTION_CHARS
    assert got["description_truncated"] is True
    assert got["description_chars"] == len(body)


async def test_a_short_description_is_not_marked_truncated() -> None:
    """The cap must be invisible below the threshold: a task whose whole
    description fits pays neither the marker keys nor a round trip."""
    token, org_id, _uid = await _org()
    tid = await _task_with_description(token, org_id, "short enough")

    got = await get_task(token=token, org_id=org_id, task_id=tid)
    assert got["description"] == "short enough"
    assert "description_truncated" not in got
    assert "description_chars" not in got


async def test_full_description_is_reachable_inline() -> None:
    """The cap is a default, never data loss. The other way to the whole text
    is ``get_text_block_capability``, which writes it to a file instead of
    into the conversation."""
    token, org_id, _uid = await _org()
    body = "y" * (_TASK_DESCRIPTION_CHARS + 500)
    tid = await _task_with_description(token, org_id, body)

    got = await get_task(token=token, org_id=org_id, task_id=tid, full_description=True)
    assert got["description"] == body
    assert "description_truncated" not in got


# --- what a row stops carrying ----------------------------------------------


async def test_list_rows_do_not_repeat_the_workflow() -> None:
    """``workflow_id`` answers a question about the page, not about the row.
    On a page of 200 tasks in one workflow it was the same uuid 200 times at
    ~23 tokens each; it is on ``get_task`` and on ``task_workflow``.

    Observed failing on 2026-09-17 with the key put back on the lean row."""
    token, org_id, _uid = await _org()
    await create_task(token=token, org_id=org_id, title="one")
    page = await list_tasks(token=token, org_id=org_id)
    assert page["items"]
    assert all("workflow_id" not in row for row in page["items"])
    assert all(row["state_id"] for row in page["items"])


async def test_resolved_handle_replaces_the_id_it_resolves() -> None:
    """Measured at zero reuse over 32 sessions: 8 ``assignee_id`` values
    returned, none ever passed back to anything, while ``set_task_assignee``
    takes the handle. The id survives only where the handle did not resolve,
    which is the one case where dropping it would strand the assignment."""
    token, org_id, _uid = await _org()
    created = await create_task(token=token, org_id=org_id, title="assigned")
    got = await get_task(token=token, org_id=org_id, task_id=created["id"])
    if got.get("owner_handle"):
        assert "owner_id" not in got
    else:
        pytest.skip("no owner handle resolved in this fixture; the fallback path is the assertion")


# --- short ids, and the surface that owns them -------------------------------


async def test_task_ids_travel_short_on_the_gateway_only() -> None:
    """A uuid costs ~23 tokens and an 8-hex prefix ~4, measured with the
    cl100k tokenizer over the recorded corpus, and 90% of the uuids this
    surface returned were never passed back to anything. The short form is the
    ADR-0038 convention already used by the ``/t/<prefix>`` URLs and by the ids
    people write into notes by hand.

    On the GATEWAY, not in the serializers, and this test asserts both sides
    of that. The first version shortened in ``server.py``, which the stdio
    registry shares, so that surface handed out an id its own ``get_task``
    then refused to parse. A surface must consume what it emits, which means
    the shortening lives with the expansion or not at all."""
    org = await _org()
    token, org_id, _uid = org
    async with _as_principal(org):
        created = await execute_tool(name="create_task", arguments={"title": "short id"})
        assert len(created["id"]) == 8

        full = await execute_tool(name="get_task", arguments={"task_id": created["id"]})
        assert full["id"] == created["id"]

        page = await execute_tool(name="list_tasks", arguments={})
    row = next(r for r in page["items"] if r["id"] == created["id"])
    assert row["id"] == full["id"]

    direct = await create_task(token=token, org_id=org_id, title="registry")
    assert len(direct["id"]) == 36


async def test_a_foreign_id_is_never_shortened() -> None:
    """The rule that keeps the short form safe: only a task or a note, the two
    kinds ``resolve_prefix`` can expand. A note's ``parts[].id`` is a part;
    nothing can resolve a prefix of one, so shortening it would be data loss
    rather than economy. It sits deeper than a top-level row, which is what
    the walk keys on."""
    org = await _org()
    async with _as_principal(org):
        note = await execute_tool(name="create_note", arguments={"kind": "text", "text": "a body"})
        got = await execute_tool(name="get_note", arguments={"note_id": note["id"]})
    assert len(got["id"]) == 8
    assert got["parts"], got
    assert all(len(p["id"]) == 36 for p in got["parts"])


async def test_a_short_id_is_accepted_wherever_it_is_handed_out() -> None:
    """Without expansion at the dispatcher a short id would be a dead end and
    the caller would pay a ``resolve_prefix`` round trip to undo an economy."""
    org = await _org()
    async with _as_principal(org):
        created = await execute_tool(name="create_task", arguments={"title": "reachable"})
        got = await execute_tool(name="get_task", arguments={"task_id": created["id"]})
    assert got["id"] == created["id"]
    assert got["title"] == "reachable"


async def test_an_unknown_prefix_is_refused_by_name() -> None:
    """An id that names nothing must say so as an id problem. Without
    expansion this reached the tool as a string and came back as a uuid parse
    error, which reads like a malformed argument rather than a missing row."""
    org = await _org()
    async with _as_principal(org):
        res = await execute_tool(name="get_task", arguments={"task_id": "ffffffff"})
    assert res["error"]["code"] == "id.prefix_unknown"
    assert res["error"]["argument"] == "task_id"


async def test_a_full_uuid_still_works_untouched() -> None:
    """The expansion must be additive. A full uuid does not match the prefix
    shape at all, so a caller that never learned about short ids takes exactly
    the path it took before."""
    org = await _org()
    token, org_id, _uid = org
    created = await create_task(token=token, org_id=org_id, title="full uuid")
    async with _as_principal(org):
        got = await execute_tool(name="get_task", arguments={"task_id": created["id"]})
    assert got["id"] == created["id"][:8]
    assert got["title"] == "full uuid"


async def test_every_entity_row_tool_exists() -> None:
    """``_ENTITY_ROW_TOOLS`` is hand-kept, and the failure it can have is a
    rename: a name that no longer exists silently stops shortening and nobody
    notices, because a full uuid still works. Asserting the names against the
    live registry turns that into a red test instead of a slow regression."""
    from mycelium_mcp.gateway import _ENTITY_ROW_TOOLS, _registry

    known = {t.name for t in _registry._tool_manager.list_tools()}
    assert _ENTITY_ROW_TOOLS <= known, sorted(_ENTITY_ROW_TOOLS - known)


async def test_the_token_free_read_accepts_the_short_id_it_was_pointed_at() -> None:
    """``get_task`` caps a long description and names
    ``get_text_block_capability(kind='task_description', resource_id=...)`` as
    the way to the whole text without spending tokens on it. The caller
    holding that advice holds a SHORT id, and the capability tool takes its id
    under ``resource_id``, whose kind is decided by the sibling ``kind``
    argument -- so without the discriminated entry the surface would point at
    a door and then refuse the key it had just handed over."""
    org = await _org()
    async with _as_principal(org):
        created = await execute_tool(name="create_task", arguments={"title": "with a brief"})
        await execute_tool(
            name="update_task",
            arguments={
                "task_id": created["id"],
                "expected_version": created["version"],
                "description": "d" * (_TASK_DESCRIPTION_CHARS + 100),
            },
        )
        capability = await execute_tool(
            name="get_text_block_capability",
            arguments={"kind": "task_description", "resource_id": created["id"]},
        )
    assert "error" not in capability, capability
    assert capability["curl"].startswith("curl ")


async def test_the_bootstrap_payload_carries_short_ids_too() -> None:
    """``whoami`` is the worst payload to miss and the easiest to forget: it
    is read at turn 1 and then sits in context for the rest of the session, so
    twelve calls in the recorded corpus carried 10% of this server's context
    weight. Its task rows arrive under ``open_tasks``, not ``items``, which is
    exactly how the first version of the walk skipped them -- found by reading
    a real payload, not by a test, which is why there is now a test."""
    org = await _org()
    async with _as_principal(org):
        await execute_tool(name="create_task", arguments={"title": "mine"})
        me = await execute_tool(name="whoami", arguments={})
    assert me["open_tasks"], me
    assert all(len(row["id"]) == 8 for row in me["open_tasks"])
    # The identity ids in the same payload are NOT tasks and stay whole.
    assert len(me["identity"]["user_id"]) == 36


async def test_the_search_meta_keeps_its_shape_when_nothing_matched() -> None:
    """``model_id`` moved from every hit to the response, and the response
    meta is asserted as a WHOLE key set by its contract test. So the key has
    to be present even when no hit answers for it: a caller reading meta must
    not have to work out whether a field is missing because nothing matched or
    because it was renamed. Null is the answer, absence is not."""
    org = await _org()
    async with _as_principal(org):
        empty = await execute_tool(
            name="search",
            arguments={"q": "zzzzzzzz-no-such-thing-zzzzzzzz", "operation_id": "econ-empty"},
        )
    assert empty["hits"] == []
    assert "model_id" in empty["meta"]
    assert empty["meta"]["model_id"] is None


def _uuid_parsed_arguments() -> dict[str, set[str]]:
    """Every ``@mcp.tool()`` argument the tool body turns into a uuid.

    Read off the source rather than the signatures, because the signature says
    ``str`` and only the body says "this is an id". This is the enumeration
    that the prefix tables are derived from; the test below keeps them in step.
    """
    import re
    from pathlib import Path

    import mycelium_mcp.server as server_mod

    src = Path(server_mod.__file__).read_text()
    out: dict[str, set[str]] = {}
    for chunk in re.split(r"\n@mcp\.tool\(\)\n", src)[1:]:
        m = re.match(r"(?:async )?def (\w+)\(([^)]*)\)", chunk, re.S)
        if not m:
            continue
        tool, params = m.group(1), m.group(2)
        body = chunk[m.end() :]
        for raw in params.split(","):
            if ":" not in raw:
                continue
            arg = raw.strip().split(":")[0].strip()
            if arg in ("token", "org_id"):
                continue
            if re.search(rf"uuid\.UUID\({arg}\b", body):
                out.setdefault(arg, set()).add(tool)
    return out


#: Uuid-carrying arguments that are NOT a task or a note, so a prefix cannot be
#: expanded into them and this surface never shortens them either. Listed by
#: hand because the decision is semantic: it is the answer to "can
#: ``resolve_prefix`` turn eight hex digits back into this?", and today it
#: resolves tasks and notes and nothing else.
_NOT_AN_ENTITY_ID = {
    "account_id",
    "annotation_id",
    "assignee_id",
    "attachment_id",
    "blob_id",
    "budget_id",
    "calendar_id",
    "channel_id",
    "channel_tag_id",
    "client_tag_id",
    "comment_id",
    "created_by",
    "dependency_id",
    "entity",
    "entry_id",
    "executor_id",
    "identity_id",
    "invoice_id",
    "issuer_profile_id",
    "item_id",
    "job_id",
    "line_id",
    "message_id",
    "owner_id",
    "parent_invoice_id",
    "part_id",
    "project_id",
    "project_tag_id",
    "relation_id",
    "request_id",
    "revision_id",
    "run_id",
    "state_id",
    "tag_id",
    "transition_to",
    "user_id",
    "worker_id",
    "workflow_id",
}


def test_every_id_argument_is_classified() -> None:
    """The guard for the gap that bit this change twice.

    A short id is only usable if the dispatcher expands it wherever that kind
    of id is taken, and the tables listing those arguments are hand-kept. Two
    entries were missed by reading names -- ``resource_id`` (the capability
    tools' id, and ``get_task`` points a caller holding a short id straight at
    it) and ``seed`` (the note a graph walk starts from, which reads like a
    parameter) -- and both surfaced as a ``ValueError: badly formed
    hexadecimal UUID string`` from deep inside an unrelated test.

    So every uuid-parsed argument must be classified: expandable, expandable
    under a discriminator, or explicitly not an entity. A NEW one fails here
    and forces the decision, instead of working until somebody passes a short
    id into it."""
    from mycelium_mcp.gateway import _PREFIX_ARGS, _PREFIX_ARGS_BY_KIND

    classified = set(_PREFIX_ARGS) | set(_PREFIX_ARGS_BY_KIND) | _NOT_AN_ENTITY_ID
    found = _uuid_parsed_arguments()
    unclassified = {arg: sorted(tools) for arg, tools in found.items() if arg not in classified}
    assert not unclassified, (
        "id arguments nobody has classified as task/note or neither: "
        f"{unclassified}. Add them to gateway._PREFIX_ARGS (or its "
        "_BY_KIND sibling) if a prefix should expand into them, or to "
        "_NOT_AN_ENTITY_ID here if resolve_prefix cannot reach that kind."
    )


#: Names in ``_PREFIX_ARGS`` that no tool takes as an ARGUMENT: they appear
#: only as keys in a result, where the same table decides what gets shortened.
#: ``list_distillation_candidates`` returns the two ends of a proposed link
#: this way (``candidates.LinkCandidate``). A caller passes them back under the
#: argument names ``link_notes`` takes, which are expandable, so the round trip
#: still closes.
_RESPONSE_ONLY_KEYS = {"src_note_id", "dst_note_id"}


def test_the_classification_lists_nothing_that_left_the_surface() -> None:
    """The other direction, which is DOC-05 for a table: a name that was
    renamed or removed leaves a dead entry behind, and a dead entry in
    ``_PREFIX_ARGS`` is an expansion that silently stopped happening.

    This one earned its place on its first run: it rejected ``src_note_id``
    and ``dst_note_id``, which had gone into the table on the strength of
    their names. They turned out to be real, but as RESULT keys rather than
    arguments -- which is a different thing, and is now written down instead
    of assumed."""
    from mycelium_mcp.gateway import _PREFIX_ARGS, _PREFIX_ARGS_BY_KIND

    found = set(_uuid_parsed_arguments()) | _RESPONSE_ONLY_KEYS
    for table, name in ((_PREFIX_ARGS, "_PREFIX_ARGS"), (_PREFIX_ARGS_BY_KIND, "_BY_KIND")):
        stale = set(table) - found
        assert not stale, f"{name} names something no tool takes or returns: {sorted(stale)}"
    assert not (_NOT_AN_ENTITY_ID - found), sorted(_NOT_AN_ENTITY_ID - found)
