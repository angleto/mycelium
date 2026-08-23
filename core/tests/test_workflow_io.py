"""Workflow interchange document (docs/adr/0052): the rules, and the
id-keeping that makes an import safe on a workflow that holds tasks.

This module is the only implementation of the format, and it is reached
by two clients that cannot check each other's work: the SPA's
Export/Import buttons and ``mycelium workflow export|import``. It can
fail in two directions and both are expensive. Too permissive and a
malformed file becomes a lifecycle that is quietly wrong, discovered
weeks later by whoever wonders why nothing can leave "in review". Too
strict and a workflow exported from a colleague's workspace will not
load at all.

The id-keeping is the part with teeth: ``update_workflow`` reconciles
states BY ID, so an import that dropped the ids would ask the database
to delete the very states the tasks are sitting in.
"""

from __future__ import annotations

import uuid

import pytest

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.services import workflow as wf
from mycelium_core.services import workflow_io as wf_io
from mycelium_core.services.auth import signup

TODO = wf_io.DocState(name="todo", is_initial=True, description="Not started")
DOING = wf_io.DocState(name="in_progress")
DONE = wf_io.DocState(name="done", is_terminal=True)
STATES = [TODO, DOING, DONE]
EDGES = [
    wf_io.DocTransition(from_state="todo", to_state="in_progress"),
    wf_io.DocTransition(from_state="in_progress", to_state="done"),
]


def _normalize(**over: object) -> wf_io.WorkflowDoc:
    kwargs: dict[str, object] = {
        "kind": wf_io.DOC_KIND,
        "version": wf_io.DOC_VERSION,
        "name": "Delivery",
        "description": "From intake to delivery",
        "states": STATES,
        "transitions": EDGES,
    }
    kwargs.update(over)
    return wf_io.normalize(**kwargs)  # type: ignore[arg-type]


def _refused(**over: object) -> MessageCode:
    with pytest.raises(DomainError) as excinfo:
        _normalize(**over)
    return excinfo.value.code


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="Org",
        )
    return r.org_id, r.user_id


def test_normalize_accepts_a_well_formed_document() -> None:
    doc = _normalize()
    assert doc.name == "Delivery"
    assert [s.name for s in doc.states] == ["todo", "in_progress", "done"]
    assert doc.states[0].is_initial is True
    assert doc.states[2].is_terminal is True
    assert doc.transitions == tuple(EDGES)


def test_normalize_is_idempotent() -> None:
    once = _normalize()
    twice = wf_io.normalize(
        kind=wf_io.DOC_KIND,
        version=wf_io.DOC_VERSION,
        name=once.name,
        description=once.description,
        states=list(once.states),
        transitions=list(once.transitions),
    )
    assert twice == once
    assert wf_io.to_json(twice) == wf_io.to_json(once)


def test_a_file_that_is_not_ours_is_refused() -> None:
    assert _refused(kind="mycelium.note") is MessageCode.WORKFLOW_DOC_KIND
    assert _refused(kind="") is MessageCode.WORKFLOW_DOC_KIND


def test_a_version_the_server_cannot_vouch_for_is_refused() -> None:
    # Forward compatibility is a promise this build cannot make: a later
    # writer may mean something different by the same field.
    assert _refused(version=2) is MessageCode.WORKFLOW_DOC_VERSION
    assert _refused(version=0) is MessageCode.WORKFLOW_DOC_VERSION


def test_the_name_is_held_to_the_width_of_its_column() -> None:
    assert _refused(name="") is MessageCode.WORKFLOW_DOC_NAME
    assert _refused(name="   ") is MessageCode.WORKFLOW_DOC_NAME
    assert _refused(name="x" * 121) is MessageCode.WORKFLOW_DOC_NAME
    # Trimmed before measuring, so a stray space is not a rejection.
    assert _normalize(name="  Delivery  ").name == "Delivery"
    assert len(_normalize(name="x" * 120).name) == 120


def test_a_workflow_with_no_states_is_refused() -> None:
    assert _refused(states=[], transitions=[]) is MessageCode.WORKFLOW_DOC_NO_STATES


def test_a_state_name_is_held_to_its_column_and_named_by_row() -> None:
    for bad in ("", "   ", "x" * 81):
        code = _refused(
            states=[TODO, wf_io.DocState(name=bad)],
            transitions=[],
        )
        assert code is MessageCode.WORKFLOW_DOC_STATE_NAME


def test_two_states_that_would_collide_on_save_are_refused() -> None:
    # (workflow_id, name) is unique; without this the insert would fail
    # as an IntegrityError nobody translated.
    code = _refused(states=[TODO, wf_io.DocState(name="  todo  ")], transitions=[])
    assert code is MessageCode.WORKFLOW_DOC_DUPLICATE_STATE


def test_exactly_one_initial_state_is_required() -> None:
    assert _refused(states=[DOING, DONE], transitions=[]) is MessageCode.WORKFLOW_DOC_INITIAL_COUNT
    both = [TODO, wf_io.DocState(name="done", is_initial=True)]
    assert _refused(states=both, transitions=[]) is MessageCode.WORKFLOW_DOC_INITIAL_COUNT


def test_a_transition_that_leads_nowhere_is_refused() -> None:
    dangling = [wf_io.DocTransition(from_state="todo", to_state="shipped")]
    assert _refused(transitions=dangling) is MessageCode.WORKFLOW_DOC_UNKNOWN_STATE
    backwards = [wf_io.DocTransition(from_state="shipped", to_state="todo")]
    assert _refused(transitions=backwards) is MessageCode.WORKFLOW_DOC_UNKNOWN_STATE
    empty = [wf_io.DocTransition(from_state="todo", to_state="")]
    assert _refused(transitions=empty) is MessageCode.WORKFLOW_DOC_UNKNOWN_STATE


def test_the_same_transition_twice_is_refused() -> None:
    twice = [
        wf_io.DocTransition(from_state="todo", to_state="in_progress"),
        wf_io.DocTransition(from_state=" todo ", to_state="in_progress"),
    ]
    assert _refused(transitions=twice) is MessageCode.WORKFLOW_DOC_DUPLICATE_TRANSITION


def test_absent_blank_and_null_description_are_the_same_silence() -> None:
    assert _normalize(description=None).description is None
    assert _normalize(description="   ").description is None
    assert _normalize(description="  useful  ").description == "useful"
    quiet = _normalize(
        states=[wf_io.DocState(name="todo", is_initial=True, description="  ")],
        transitions=[],
    )
    assert quiet.states[0].description is None


def test_a_state_may_loop_back_to_itself() -> None:
    # Allowed by the schema and by the service; a "re-open in place"
    # edge is a legitimate lifecycle, not a mistake to guess away.
    loop = [wf_io.DocTransition(from_state="in_progress", to_state="in_progress")]
    assert _normalize(transitions=loop).transitions == tuple(loop)


def test_a_workflow_with_no_transitions_is_a_valid_document() -> None:
    assert _normalize(transitions=[]).transitions == ()


async def test_export_round_trips_through_import() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        created = await wf_io.import_as_new_workflow(s, org_id=org, actor_id=user, doc=_normalize())
        wf_id = created.id
    async with tenant_session(str(org), str(user)) as s:
        doc = await wf_io.export_workflow(s, workflow_id=wf_id)
    assert doc.name == "Delivery"
    assert [x.name for x in doc.states] == ["todo", "in_progress", "done"]
    assert doc.states[0].is_initial is True
    assert doc.states[0].description == "Not started"
    assert set(doc.transitions) == set(EDGES)
    # The exported document carries no database identity: it is the same
    # object a second workspace would receive.
    payload = wf_io.to_json(doc)
    assert payload["kind"] == wf_io.DOC_KIND
    assert "id" not in payload
    assert all("id" not in st for st in payload["states"])


async def test_import_keeps_the_id_of_every_state_it_names_again() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        created = await wf_io.import_as_new_workflow(s, org_id=org, actor_id=user, doc=_normalize())
        wf_id = created.id
    async with tenant_session(str(org), str(user)) as s:
        before = {st.name: st.id for st in await wf.get_states(s, wf_id)}

    # Re-import the same shape plus one new state: everything named
    # again keeps its row, so anything sitting in those states stays put.
    grown = _normalize(
        states=[*STATES, wf_io.DocState(name="in_review")],
        transitions=[*EDGES, wf_io.DocTransition(from_state="in_progress", to_state="in_review")],
    )
    async with tenant_session(str(org), str(user)) as s:
        await wf_io.import_into_workflow(s, org_id=org, actor_id=user, workflow_id=wf_id, doc=grown)
    async with tenant_session(str(org), str(user)) as s:
        after = {st.name: st.id for st in await wf.get_states(s, wf_id)}

    assert {n: after[n] for n in before} == before
    assert set(after) == {"todo", "in_progress", "done", "in_review"}


async def test_import_reorders_states_to_the_document_order() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        created = await wf_io.import_as_new_workflow(s, org_id=org, actor_id=user, doc=_normalize())
        wf_id = created.id
    # RULE 2: position in the list IS ord, so a reordered file reorders
    # the board rather than being silently ignored.
    flipped = _normalize(states=[DONE, DOING, wf_io.DocState(name="todo", is_initial=True)])
    async with tenant_session(str(org), str(user)) as s:
        await wf_io.import_into_workflow(
            s, org_id=org, actor_id=user, workflow_id=wf_id, doc=flipped
        )
    async with tenant_session(str(org), str(user)) as s:
        ordered = [st.name for st in await wf.get_states(s, wf_id)]
    assert ordered == ["done", "in_progress", "todo"]


async def test_a_state_the_document_drops_is_deleted() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        created = await wf_io.import_as_new_workflow(s, org_id=org, actor_id=user, doc=_normalize())
        wf_id = created.id
    shrunk = _normalize(
        states=[TODO, DONE],
        transitions=[wf_io.DocTransition(from_state="todo", to_state="done")],
    )
    async with tenant_session(str(org), str(user)) as s:
        await wf_io.import_into_workflow(
            s, org_id=org, actor_id=user, workflow_id=wf_id, doc=shrunk
        )
    async with tenant_session(str(org), str(user)) as s:
        assert {st.name for st in await wf.get_states(s, wf_id)} == {"todo", "done"}


async def test_the_name_override_lets_one_file_be_imported_twice() -> None:
    # (org_id, name) is unique on workflow_defs, so without the override
    # importing a colleague's "Delivery" a second time could only fail.
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        first = await wf_io.import_as_new_workflow(s, org_id=org, actor_id=user, doc=_normalize())
        second = await wf_io.import_as_new_workflow(
            s, org_id=org, actor_id=user, doc=_normalize(), name="Delivery (copy)"
        )
    assert first.name == "Delivery"
    assert second.name == "Delivery (copy)"
    assert first.id != second.id


async def test_an_imported_workflow_never_arrives_as_the_default() -> None:
    # is_default belongs to the workspace's configuration, not to the
    # file: promoting one stays a separate, audited action.
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        created = await wf_io.import_as_new_workflow(s, org_id=org, actor_id=user, doc=_normalize())
        assert created.is_default is False
        assert (await wf.get_default_workflow(s, org)).id != created.id
