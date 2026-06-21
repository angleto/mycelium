"""A transplanted note is read-only on every content mutator (task
e0648a4c, from annotations backlog 1f161485 #3).

Invariant (docs/adr/0029 D2): once a note is promoted to a task
(``promoted_at IS NOT NULL``) the service layer treats it as read-only.
``note_links`` already guarded ``set_maturity`` / linking, but the body/part
mutators did not -- so a promoted note's content could still be edited. These
tests pin the guard (``NOTE_PROMOTED_READONLY``) on EVERY content mutator and
confirm a normal note is unaffected.
"""

from __future__ import annotations

import datetime as dt
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flow_core.db import admin_session, tenant_session  # noqa: E402
from flow_core.errors import DomainError  # noqa: E402
from flow_core.i18n import MessageCode  # noqa: E402
from flow_core.models.note import Note, NoteKind  # noqa: E402
from flow_core.models.note_part import NotePart  # noqa: E402
from flow_core.services import note_parts as np  # noqa: E402
from flow_core.services import notes as nt  # noqa: E402
from flow_core.services.auth import signup  # noqa: E402


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="PROM")
    return r.org_id, r.user_id


async def _note_with_part(s: object, org: uuid.UUID, user: uuid.UUID) -> tuple[Note, NotePart]:
    note = await nt.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title="t",
        text="body one two three",
    )
    parts = await np.list_parts(s, org_id=org, note_id=note.id)  # type: ignore[arg-type]
    return note, parts[0]


async def _promote(s: object, note: Note) -> None:
    """Simulate the read-only transplant side-effect directly (the guard only
    reads ``promoted_at``; this avoids dragging in the whole task-creation
    flow)."""
    note.promoted_at = dt.datetime.now(dt.UTC)
    await s.flush()  # type: ignore[attr-defined]


def _is_promoted_readonly(exc: pytest.ExceptionInfo[DomainError]) -> bool:
    return exc.value.code == MessageCode.NOTE_PROMOTED_READONLY


async def test_every_content_mutator_refuses_a_promoted_note() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note, part = await _note_with_part(s, org, user)
        target, _ = await _note_with_part(s, org, user)
        await _promote(s, note)
        v = note.version
        pv = part.version

        with pytest.raises(DomainError) as e:
            await nt.update_note(
                s, org_id=org, actor_id=user, note_id=note.id, expected_version=v, text="x"
            )
        assert _is_promoted_readonly(e)
        with pytest.raises(DomainError) as e:
            await np.create_part(s, org_id=org, actor_id=user, note_id=note.id, body="x")
        assert _is_promoted_readonly(e)
        with pytest.raises(DomainError) as e:
            await np.update_part(
                s, org_id=org, actor_id=user, part_id=part.id, expected_version=pv, body="x"
            )
        assert _is_promoted_readonly(e)
        with pytest.raises(DomainError) as e:
            await np.append_to_part(
                s, org_id=org, actor_id=user, part_id=part.id, chunk="x", expected_version=pv
            )
        assert _is_promoted_readonly(e)
        with pytest.raises(DomainError) as e:
            await np.prepend_to_part(
                s, org_id=org, actor_id=user, part_id=part.id, text="x", expected_version=pv
            )
        assert _is_promoted_readonly(e)
        with pytest.raises(DomainError) as e:
            await np.replace_in_part(
                s,
                org_id=org,
                actor_id=user,
                part_id=part.id,
                find="body",
                replace="z",
                expected_version=pv,
            )
        assert _is_promoted_readonly(e)
        with pytest.raises(DomainError) as e:
            await np.reorder_parts(
                s, org_id=org, actor_id=user, note_id=note.id, part_ids=[part.id]
            )
        assert _is_promoted_readonly(e)
        # merge is blocked whether the promoted note is the source or the target.
        with pytest.raises(DomainError) as e:
            await np.merge_notes(
                s, org_id=org, actor_id=user, source_note_id=note.id, target_note_id=target.id
            )
        assert _is_promoted_readonly(e)
        with pytest.raises(DomainError) as e:
            await np.merge_notes(
                s, org_id=org, actor_id=user, source_note_id=target.id, target_note_id=note.id
            )
        assert _is_promoted_readonly(e)
        # delete_part last (it would otherwise remove the row under the others).
        with pytest.raises(DomainError) as e:
            await np.delete_part(s, org_id=org, actor_id=user, part_id=part.id)
        assert _is_promoted_readonly(e)


async def test_non_promoted_note_content_mutators_still_work() -> None:
    """Control: the guard does not touch an ordinary (non-promoted) note."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note, part = await _note_with_part(s, org, user)
        # title/body edit lands (assert the effect, not version arithmetic).
        await nt.update_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=note.id,
            expected_version=note.version,
            text="edited body",
        )
        assert "edited body" in (await nt.get_body(s, note_id=note.id) or "")
        # a fresh part
        extra = await np.create_part(
            s, org_id=org, actor_id=user, note_id=note.id, body="second part"
        )
        assert extra.id is not None
        # append to the original part
        _pv, appended = await np.append_to_part(
            s,
            org_id=org,
            actor_id=user,
            part_id=part.id,
            chunk=" more",
            expected_version=part.version,
        )
        assert appended == len(" more")
