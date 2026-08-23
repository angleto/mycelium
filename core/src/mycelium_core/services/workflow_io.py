"""Workflow interchange document (docs/adr/0052): the portable form of
a ``WorkflowDefinition``, and the only implementation of its rules.

The rules live here, in the service layer, and not in whichever client
happens to need them, because there is more than one client: the SPA's
Export/Import buttons and ``mycelium workflow export|import`` are two
front ends onto the same three functions. A second implementation would
be a second answer to "is this file valid", and the two would drift.

RULE 1 -- the document carries NO database identity. No workflow id, no
state ids, no ``org_id``, no optimistic-lock version, no ``is_default``.
A state is addressed by its NAME and a transition names the two states
it joins. That is what lets a file leave the workspace it was exported
from; none of those ids mean anything anywhere else.

RULE 2 -- the position of a state in ``states`` IS its ``ord``. Storing
it twice would create two sources of truth free to disagree.

RULE 3 -- import into an existing workflow matches states BY NAME and
keeps their ids. ``workflow.update_workflow`` reconciles BY ID: a state
row without one is an insert, and an existing state missing from the
payload is a delete that is refused while tasks occupy it. Dropping the
ids would therefore either move every task out from under its state or
make the save impossible. See ``_reuse_ids``.

RULE 4 -- refuse, do not coerce. Every rule below raises a distinct
``MessageCode`` naming the offending row, because the caller has a file
in front of them and needs to know which line to fix. A document that
merely looks plausible must not become a lifecycle that is quietly
wrong.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.workflow import WorkflowDefinition
from mycelium_core.services import audit
from mycelium_core.services import workflow as wf

#: Marks the file as ours. A JSON file without it is refused rather than
#: hopefully coerced.
DOC_KIND = "mycelium.workflow"

#: Bumped only for a change this parser could not read correctly.
#: Adding an optional field does not qualify: unknown keys are ignored.
DOC_VERSION = 1

# Mirror the ``String(120)`` / ``String(80)`` columns of
# models/workflow.py. Checked here rather than in the pydantic schema so
# the limit is applied to the TRIMMED name, and so there is one rule
# rather than two that can disagree by a stripped space.
WORKFLOW_NAME_MAX = 120
STATE_NAME_MAX = 80


@dataclass(frozen=True, slots=True)
class DocState:
    name: str
    is_initial: bool = False
    is_terminal: bool = False
    is_hidden: bool = False
    description: str | None = None


@dataclass(frozen=True, slots=True)
class DocTransition:
    from_state: str
    to_state: str


@dataclass(frozen=True, slots=True)
class WorkflowDoc:
    name: str
    description: str | None
    states: tuple[DocState, ...]
    transitions: tuple[DocTransition, ...]


def _text(value: str | None) -> str | None:
    """Absent, blank and null description are one and the same thing:
    nothing to say. Collapsing them keeps a round trip from turning ""
    into null into "" again."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def normalize(
    *,
    kind: str,
    version: int,
    name: str,
    description: str | None,
    states: Sequence[DocState],
    transitions: Sequence[DocTransition],
) -> WorkflowDoc:
    """Validate a decoded document and return it in canonical form.

    Raises ``DomainError`` with the code for the first rule broken. The
    caller passes already-decoded values (the API's pydantic body, the
    row set built by ``export_workflow``), so this function is about
    MEANING: shape errors never reach it.
    """
    if kind != DOC_KIND:
        raise DomainError(MessageCode.WORKFLOW_DOC_KIND, expected=DOC_KIND, got=kind)
    if not 1 <= version <= DOC_VERSION:
        raise DomainError(MessageCode.WORKFLOW_DOC_VERSION, version=version, supported=DOC_VERSION)

    doc_name = name.strip()
    if not doc_name or len(doc_name) > WORKFLOW_NAME_MAX:
        raise DomainError(MessageCode.WORKFLOW_DOC_NAME, maximum=WORKFLOW_NAME_MAX)
    if not states:
        raise DomainError(MessageCode.WORKFLOW_DOC_NO_STATES)

    seen: set[str] = set()
    clean_states: list[DocState] = []
    for index, spec in enumerate(states):
        position = index + 1
        state_name = spec.name.strip()
        if not state_name or len(state_name) > STATE_NAME_MAX:
            raise DomainError(
                MessageCode.WORKFLOW_DOC_STATE_NAME,
                position=position,
                maximum=STATE_NAME_MAX,
            )
        if state_name in seen:
            raise DomainError(MessageCode.WORKFLOW_DOC_DUPLICATE_STATE, name=state_name)
        seen.add(state_name)
        clean_states.append(
            DocState(
                name=state_name,
                is_initial=spec.is_initial,
                is_terminal=spec.is_terminal,
                is_hidden=spec.is_hidden,
                description=_text(spec.description),
            )
        )

    initial = sum(1 for s in clean_states if s.is_initial)
    if initial != 1:
        raise DomainError(MessageCode.WORKFLOW_DOC_INITIAL_COUNT, found=initial)

    edges: set[tuple[str, str]] = set()
    clean_transitions: list[DocTransition] = []
    for index, edge in enumerate(transitions):
        position = index + 1
        src, dst = edge.from_state.strip(), edge.to_state.strip()
        for endpoint in (src, dst):
            if endpoint not in seen:
                raise DomainError(
                    MessageCode.WORKFLOW_DOC_UNKNOWN_STATE, position=position, name=endpoint
                )
        # The (workflow_id, from_state_id, to_state_id) unique constraint
        # would otherwise surface as an unhandled IntegrityError on save.
        if (src, dst) in edges:
            raise DomainError(
                MessageCode.WORKFLOW_DOC_DUPLICATE_TRANSITION, from_state=src, to_state=dst
            )
        edges.add((src, dst))
        clean_transitions.append(DocTransition(from_state=src, to_state=dst))

    return WorkflowDoc(
        name=doc_name,
        description=_text(description),
        states=tuple(clean_states),
        transitions=tuple(clean_transitions),
    )


def to_json(doc: WorkflowDoc) -> dict[str, Any]:
    """The document as the object a file holds. Key order is fixed so
    re-exporting an unchanged workflow produces an identical file."""
    return {
        "kind": DOC_KIND,
        "version": DOC_VERSION,
        "name": doc.name,
        "description": doc.description,
        "states": [
            {
                "name": s.name,
                "is_initial": s.is_initial,
                "is_terminal": s.is_terminal,
                "is_hidden": s.is_hidden,
                "description": s.description,
            }
            for s in doc.states
        ],
        "transitions": [
            {"from_state": t.from_state, "to_state": t.to_state} for t in doc.transitions
        ],
    }


async def export_workflow(session: AsyncSession, *, workflow_id: uuid.UUID) -> WorkflowDoc:
    """Read a stored workflow back out as a portable document.

    Goes through ``normalize`` like any other document: a workflow the
    exporter cannot describe legally is a bug worth surfacing here, not
    a file that fails on the machine it was carried to.
    """
    definition = await wf.get_workflow(session, workflow_id)
    states = await wf.get_states(session, workflow_id)
    name_by_id = {s.id: s.name for s in states}
    edges = await wf.list_transitions(session, workflow_id)
    return normalize(
        kind=DOC_KIND,
        version=DOC_VERSION,
        name=definition.name,
        description=definition.description,
        states=[
            DocState(
                name=s.name,
                is_initial=s.is_initial,
                is_terminal=s.is_terminal,
                is_hidden=s.is_hidden,
                description=s.description,
            )
            for s in states
        ],
        transitions=[
            DocTransition(
                from_state=name_by_id[e.from_state_id],
                to_state=name_by_id[e.to_state_id],
            )
            for e in edges
            if e.from_state_id in name_by_id and e.to_state_id in name_by_id
        ],
    )


def _reuse_ids(
    doc: WorkflowDoc, existing: Sequence[tuple[uuid.UUID, str]]
) -> tuple[list[wf.StateEdit], dict[str, list[str]]]:
    """Turn the document's states into ``StateEdit`` rows, carrying over
    the id of every state the document names again (RULE 3).

    Also returns what changed, for the audit ``diff``: an import is a
    bulk edit whose interesting part is which states survived, which
    were added, and which the document dropped.
    """
    id_by_name: dict[str, uuid.UUID] = {}
    for existing_id, existing_name in existing:
        id_by_name.setdefault(existing_name, existing_id)
    kept: list[str] = []
    added: list[str] = []
    edits: list[wf.StateEdit] = []
    for ord_, state in enumerate(doc.states):
        matched = id_by_name.get(state.name)
        (kept if matched is not None else added).append(state.name)
        edits.append(
            wf.StateEdit(
                id=matched,
                name=state.name,
                ord=ord_,
                is_initial=state.is_initial,
                is_terminal=state.is_terminal,
                is_hidden=state.is_hidden,
                description=state.description,
            )
        )
    named = {s.name for s in doc.states}
    removed = [state_name for _, state_name in existing if state_name not in named]
    return edits, {"kept": kept, "added": added, "removed": removed}


async def import_into_workflow(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    workflow_id: uuid.UUID,
    doc: WorkflowDoc,
) -> None:
    """Replace an existing workflow's configuration with the document.

    Delegates the write to ``update_workflow`` rather than touching the
    tables: the RBAC check, the "exactly one initial state" invariant,
    the refusal to delete a state that still holds tasks and the audit
    entry are all its job, and an importer that bypassed them would be a
    second, weaker door onto the same data.
    """
    states = await wf.get_states(session, workflow_id)
    edits, changed = _reuse_ids(doc, [(s.id, s.name) for s in states])
    await wf.update_workflow(
        session,
        org_id=org_id,
        actor_id=actor_id,
        workflow_id=workflow_id,
        name=doc.name,
        description=doc.description,
        states=edits,
        transitions=[(t.from_state, t.to_state) for t in doc.transitions],
    )
    # A second entry beside update_workflow's own: the fact that this
    # edit arrived as a file, and what it did to the state set, is what
    # an operator reading the log later needs.
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="workflow",
        entity_id=workflow_id,
        action="import",
        diff={"states": changed},
    )


async def import_as_new_workflow(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    doc: WorkflowDoc,
    name: str | None = None,
) -> WorkflowDefinition:
    """Create a new workflow from the document.

    ``name`` overrides the one in the file, which is what makes a file
    importable twice into the same workspace: ``workflow_defs`` is
    unique on ``(org_id, name)``, so without the override the second
    import of a colleague's "Delivery" could only fail.
    """
    doc_name = (name or doc.name).strip()
    if not doc_name or len(doc_name) > WORKFLOW_NAME_MAX:
        raise DomainError(MessageCode.WORKFLOW_DOC_NAME, maximum=WORKFLOW_NAME_MAX)
    definition = await wf.create_workflow(
        session,
        org_id=org_id,
        actor_id=actor_id,
        name=doc_name,
        description=doc.description,
        states=[
            wf.StateSpec(
                name=s.name,
                ord=ord_,
                is_initial=s.is_initial,
                is_terminal=s.is_terminal,
                is_hidden=s.is_hidden,
                description=s.description,
            )
            for ord_, s in enumerate(doc.states)
        ],
        transitions=[(t.from_state, t.to_state) for t in doc.transitions],
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="workflow",
        entity_id=definition.id,
        action="import",
        diff={"states": {"kept": [], "added": [s.name for s in doc.states], "removed": []}},
    )
    return definition
