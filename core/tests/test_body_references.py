"""The written-reference projection (ADR-0073), against the database.

Freshness is the property the design leans on: a backlink exists exactly
when the body that holds it has committed. So there is one test per write
function of ``note_parts``, ``notes`` and ``tasks`` that can change a body,
and :func:`test_every_body_writer_has_a_freshness_test` fails when a new one
appears without its test. Each test writes in one transaction and reads in
the next, which is where the commit-time flush has landed.
"""

from __future__ import annotations

import difflib
import inspect
import re
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import select, update

from mycelium_core.ai_providers import TranscriptResult
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.errors import ConflictError, UnprocessableError
from mycelium_core.models.body_reference import BodyReference, BodyReferenceSource
from mycelium_core.models.note import Note, NoteKind
from mycelium_core.models.note_part import NotePart
from mycelium_core.models.note_tag import NoteTag
from mycelium_core.services import billing, text_patch
from mycelium_core.services import body_references as br
from mycelium_core.services import note_parts as np
from mycelium_core.services import notes as nt
from mycelium_core.services import tasks as ts
from mycelium_core.services.auth import signup


@pytest.fixture(autouse=True)
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


@dataclass(frozen=True)
class W:
    org: uuid.UUID
    user: uuid.UUID


async def _ws() -> W:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="REF",
        )
    return W(r.org_id, r.user_id)


async def _note(w: W, text: str = "target note", title: str = "Target") -> uuid.UUID:
    async with tenant_session(str(w.org), str(w.user)) as s:
        n = await nt.create_note(
            s, org_id=w.org, actor_id=w.user, kind=NoteKind.text, title=title, text=text
        )
        return n.id


async def _task(w: W, description: str | None = None, title: str = "Target task") -> uuid.UUID:
    async with tenant_session(str(w.org), str(w.user)) as s:
        t = await ts.create_task(
            s, org_id=w.org, actor_id=w.user, title=title, description=description
        )
        return t.id


async def _part0(w: W, note_id: uuid.UUID) -> NotePart:
    async with tenant_session(str(w.org), str(w.user)) as s:
        return (await np.list_parts(s, org_id=w.org, note_id=note_id))[0]


async def _task_version(w: W, task_id: uuid.UUID) -> int:
    async with tenant_session(str(w.org), str(w.user)) as s:
        return int((await ts.get_task(s, org_id=w.org, task_id=task_id)).version)


async def _backlinks(w: W, kind: br.TargetKind, target: uuid.UUID) -> list[br.WrittenBacklink]:
    async with tenant_session(str(w.org), str(w.user)) as s:
        return await br.written_backlinks(s, org_id=w.org, target_kind=kind, target_id=target)


def _chip(target: uuid.UUID) -> str:
    return f"`{str(target)[:8]}`"


def _udiff(a: str, b: str) -> str:
    return "".join(difflib.unified_diff(a.splitlines(keepends=True), b.splitlines(keepends=True)))


# ---------------------------------------------------------------- freshness

# Each writer puts a chip naming the target note into some source and
# returns the id of the note or task that now cites it.
Writer = Callable[[W, uuid.UUID], Awaitable[uuid.UUID]]


async def _w_create_note(w: W, target: uuid.UUID) -> uuid.UUID:
    return await _note(w, text=f"cites {_chip(target)} here", title="Citing")


async def _w_update_note(w: W, target: uuid.UUID) -> uuid.UUID:
    nid = await _note(w, text="nothing yet", title="Citing")
    async with tenant_session(str(w.org), str(w.user)) as s:
        n = await nt.get_note(s, org_id=w.org, note_id=nid)
        await nt.update_note(
            s,
            org_id=w.org,
            actor_id=w.user,
            note_id=nid,
            expected_version=n.version,
            text=f"now cites {_chip(target)}",
        )
    return nid


async def _w_append_to_note_field(w: W, target: uuid.UUID) -> uuid.UUID:
    nid = await _note(w, text="start", title="Citing")
    async with tenant_session(str(w.org), str(w.user)) as s:
        await nt.append_to_note_field(
            s,
            org_id=w.org,
            actor_id=w.user,
            note_id=nid,
            target="transcript",
            text=f"and {_chip(target)}",
        )
    return nid


class _CitingSTT:
    model_id = "fake-stt"

    def __init__(self, text: str) -> None:
        self._text = text

    async def transcribe(self, **_: object) -> TranscriptResult:
        return TranscriptResult(text=self._text, model_id=self.model_id, audio_seconds=1)


async def _w_transcribe(w: W, target: uuid.UUID) -> uuid.UUID:
    async with tenant_session(str(w.org), str(w.user)) as s:
        await billing.grant_credits(s, org_id=w.org, actor_id=w.user, amount=Decimal(1000))
        await billing.upsert_rate_card(
            s,
            org_id=w.org,
            actor_id=w.user,
            model_id="fake-stt",
            provider="local",
            values={
                "credits_per_input": Decimal("0.001"),
                "credits_per_output": Decimal("0.001"),
            },
        )
        n = await nt.create_note(
            s,
            org_id=w.org,
            actor_id=w.user,
            kind=NoteKind.voice,
            audio_ref="s3://audio/a.webm",
            audio_seconds=1,
        )
        await nt.transcribe(
            s,
            org_id=w.org,
            actor_id=w.user,
            note_id=n.id,
            operation_id=f"tr-{uuid.uuid4().hex}",
            embed=False,
            stt=_CitingSTT(f"spoken {str(target)[:8]} code"),
        )
        return n.id


async def _w_create_part(w: W, target: uuid.UUID) -> uuid.UUID:
    nid = await _note(w, text="first", title="Citing")
    async with tenant_session(str(w.org), str(w.user)) as s:
        await np.create_part(
            s, org_id=w.org, actor_id=w.user, note_id=nid, body=f"part {_chip(target)}"
        )
    return nid


async def _w_update_part(w: W, target: uuid.UUID) -> uuid.UUID:
    nid = await _note(w, text="first", title="Citing")
    p = await _part0(w, nid)
    async with tenant_session(str(w.org), str(w.user)) as s:
        await np.update_part(
            s,
            org_id=w.org,
            actor_id=w.user,
            part_id=p.id,
            expected_version=p.version,
            body=f"rewritten {_chip(target)}",
        )
    return nid


async def _w_append_to_part(w: W, target: uuid.UUID) -> uuid.UUID:
    nid = await _note(w, text="first", title="Citing")
    p = await _part0(w, nid)
    async with tenant_session(str(w.org), str(w.user)) as s:
        await np.append_to_part(
            s,
            org_id=w.org,
            actor_id=w.user,
            part_id=p.id,
            chunk=f" then {_chip(target)}",
            expected_version=p.version,
        )
    return nid


async def _w_prepend_to_part(w: W, target: uuid.UUID) -> uuid.UUID:
    nid = await _note(w, text="first", title="Citing")
    p = await _part0(w, nid)
    async with tenant_session(str(w.org), str(w.user)) as s:
        await np.prepend_to_part(
            s,
            org_id=w.org,
            actor_id=w.user,
            part_id=p.id,
            text=f"{_chip(target)} before ",
            expected_version=p.version,
        )
    return nid


async def _w_replace_in_part(w: W, target: uuid.UUID) -> uuid.UUID:
    nid = await _note(w, text="see PLACEHOLDER now", title="Citing")
    p = await _part0(w, nid)
    async with tenant_session(str(w.org), str(w.user)) as s:
        await np.replace_in_part(
            s,
            org_id=w.org,
            actor_id=w.user,
            part_id=p.id,
            find="PLACEHOLDER",
            replace=_chip(target),
            expected_version=p.version,
        )
    return nid


async def _w_apply_patch_to_part(w: W, target: uuid.UUID) -> uuid.UUID:
    base = "line one\nline two\n"
    nid = await _note(w, text=base, title="Citing")
    p = await _part0(w, nid)
    new = f"line one\nline {_chip(target)}\n"
    async with tenant_session(str(w.org), str(w.user)) as s:
        await np.apply_patch_to_part(
            s,
            org_id=w.org,
            actor_id=w.user,
            part_id=p.id,
            expected_version=p.version,
            patch=_udiff(p.body, new),
            base_sha256=text_patch.body_sha256(p.body),
        )
    return nid


async def _w_restore_part(w: W, target: uuid.UUID) -> uuid.UUID:
    nid = await _note(w, text="first", title="Citing")
    async with tenant_session(str(w.org), str(w.user)) as s:
        part = await np.create_part(
            s, org_id=w.org, actor_id=w.user, note_id=nid, body=f"kept {_chip(target)}"
        )
        pid = part.id
    async with tenant_session(str(w.org), str(w.user)) as s:
        await np.trash_part(s, org_id=w.org, actor_id=w.user, part_id=pid)
    assert [b.note_id for b in await _backlinks(w, "note", target)] == []
    async with tenant_session(str(w.org), str(w.user)) as s:
        await np.restore_part(s, org_id=w.org, actor_id=w.user, part_id=pid)
    return nid


async def _w_merge_notes(w: W, target: uuid.UUID) -> uuid.UUID:
    src = await _note(w, text=f"merged {_chip(target)}", title="Source")
    dst = await _note(w, text="destination", title="Citing")
    async with tenant_session(str(w.org), str(w.user)) as s:
        await np.merge_notes(
            s, org_id=w.org, actor_id=w.user, source_note_id=src, target_note_id=dst
        )
    return dst


async def _w_create_task(w: W, target: uuid.UUID) -> uuid.UUID:
    return await _task(w, description=f"see {_chip(target)}", title="Citing task")


async def _w_update_task(w: W, target: uuid.UUID) -> uuid.UUID:
    tid = await _task(w, description="nothing", title="Citing task")
    v = await _task_version(w, tid)
    async with tenant_session(str(w.org), str(w.user)) as s:
        await ts.update_task(
            s,
            org_id=w.org,
            actor_id=w.user,
            task_id=tid,
            expected_version=v,
            values={"description": f"now {_chip(target)}"},
        )
    return tid


async def _w_append_to_description(w: W, target: uuid.UUID) -> uuid.UUID:
    tid = await _task(w, description="start", title="Citing task")
    v = await _task_version(w, tid)
    async with tenant_session(str(w.org), str(w.user)) as s:
        await ts.append_to_description(
            s,
            org_id=w.org,
            actor_id=w.user,
            task_id=tid,
            text=f" {_chip(target)}",
            expected_version=v,
        )
    return tid


async def _w_prepend_to_description(w: W, target: uuid.UUID) -> uuid.UUID:
    tid = await _task(w, description="start", title="Citing task")
    v = await _task_version(w, tid)
    async with tenant_session(str(w.org), str(w.user)) as s:
        await ts.prepend_to_description(
            s,
            org_id=w.org,
            actor_id=w.user,
            task_id=tid,
            text=f"{_chip(target)} ",
            expected_version=v,
        )
    return tid


async def _w_replace_in_description(w: W, target: uuid.UUID) -> uuid.UUID:
    tid = await _task(w, description="see PLACEHOLDER", title="Citing task")
    v = await _task_version(w, tid)
    async with tenant_session(str(w.org), str(w.user)) as s:
        await ts.replace_in_description(
            s,
            org_id=w.org,
            actor_id=w.user,
            task_id=tid,
            find="PLACEHOLDER",
            replace=_chip(target),
            expected_version=v,
        )
    return tid


async def _w_apply_patch_to_description(w: W, target: uuid.UUID) -> uuid.UUID:
    base = "line one\nline two\n"
    tid = await _task(w, description=base, title="Citing task")
    v = await _task_version(w, tid)
    async with tenant_session(str(w.org), str(w.user)) as s:
        await ts.apply_patch_to_description(
            s,
            org_id=w.org,
            actor_id=w.user,
            task_id=tid,
            expected_version=v,
            patch=_udiff(base, f"line one\nline {_chip(target)}\n"),
            base_sha256=text_patch.body_sha256(base),
        )
    return tid


NOTE_WRITERS: dict[str, Writer] = {
    "notes.create_note": _w_create_note,
    "notes.update_note": _w_update_note,
    "notes.append_to_note_field": _w_append_to_note_field,
    "notes.transcribe": _w_transcribe,
    "note_parts.create_part": _w_create_part,
    "note_parts.update_part": _w_update_part,
    "note_parts.append_to_part": _w_append_to_part,
    "note_parts.prepend_to_part": _w_prepend_to_part,
    "note_parts.replace_in_part": _w_replace_in_part,
    "note_parts.apply_patch_to_part": _w_apply_patch_to_part,
    "note_parts.restore_part": _w_restore_part,
    "note_parts.merge_notes": _w_merge_notes,
}
TASK_WRITERS: dict[str, Writer] = {
    "tasks.create_task": _w_create_task,
    "tasks.update_task": _w_update_task,
    "tasks.append_to_description": _w_append_to_description,
    "tasks.prepend_to_description": _w_prepend_to_description,
    "tasks.replace_in_description": _w_replace_in_description,
    "tasks.apply_patch_to_description": _w_apply_patch_to_description,
}
# Functions that the enumeration below flags but that write no body, each
# with the reason. Removal (trash, delete) is covered separately.
NOT_BODY_WRITERS: dict[str, str] = {
    "note_parts.reorder_parts": "changes ord only; the body and its digest are unchanged",
    "tasks.add_comment": "writes a comment row, not the description",
    "tasks.list_comments": "a read",
    "tasks.list_tasks": "a read",
    "tasks.set_schedule_fields": "schedule columns only",
}


@pytest.mark.parametrize("name", sorted(NOTE_WRITERS))
async def test_note_writer_projects_before_commit(name: str) -> None:
    w = await _ws()
    target = await _note(w)
    citing = await NOTE_WRITERS[name](w, target)
    links = await _backlinks(w, "note", target)
    assert [(b.note_id, b.status, b.syntax) for b in links] == [
        (citing, "resolved", "bare" if name == "notes.transcribe" else "chip")
    ]


@pytest.mark.parametrize("name", sorted(TASK_WRITERS))
async def test_task_writer_projects_before_commit(name: str) -> None:
    w = await _ws()
    target = await _note(w)
    citing = await TASK_WRITERS[name](w, target)
    links = await _backlinks(w, "note", target)
    assert [(b.task_id, b.status, b.source_kind) for b in links] == [(citing, "resolved", "task")]


def test_every_body_writer_has_a_freshness_test() -> None:
    """A new public function that writes a part body or a task description
    must get a test above, or a stated reason here. The pattern is the
    vocabulary those writes use today; it over-matches on purpose."""
    patterns = {
        np: re.compile(r"NotePart\(|update\(NotePart\)|\.body\s*=|mark_note_part_dirty"),
        nt: re.compile(r"NotePart\(|update\(NotePart\)|\.body\s*=|_upsert_part_zero|parts_svc\."),
        ts: re.compile(r"description"),
    }
    covered = set(NOTE_WRITERS) | set(TASK_WRITERS) | set(NOT_BODY_WRITERS)
    missing: list[str] = []
    for mod, pat in patterns.items():
        short = mod.__name__.rsplit(".", 1)[-1]
        for name, fn in inspect.getmembers(mod, inspect.iscoroutinefunction):
            if name.startswith("_") or fn.__module__ != mod.__name__:
                continue
            if pat.search(inspect.getsource(fn)) and f"{short}.{name}" not in covered:
                missing.append(f"{short}.{name}")
    assert missing == []


# ---------------------------------------------------------------- removal


async def test_trashing_and_deleting_a_part_remove_its_references() -> None:
    w = await _ws()
    target = await _note(w)
    nid = await _note(w, text="first", title="Citing")
    async with tenant_session(str(w.org), str(w.user)) as s:
        a = await np.create_part(s, org_id=w.org, actor_id=w.user, note_id=nid, body=_chip(target))
        b = await np.create_part(s, org_id=w.org, actor_id=w.user, note_id=nid, body=_chip(target))
        a_id, b_id = a.id, b.id
    assert len(await _backlinks(w, "note", target)) == 2
    async with tenant_session(str(w.org), str(w.user)) as s:
        await np.trash_part(s, org_id=w.org, actor_id=w.user, part_id=a_id)
    assert len(await _backlinks(w, "note", target)) == 1
    async with tenant_session(str(w.org), str(w.user)) as s:
        await np.delete_part(s, org_id=w.org, actor_id=w.user, part_id=b_id)
    assert await _backlinks(w, "note", target) == []


async def test_removing_the_text_removes_the_reference() -> None:
    w = await _ws()
    target = await _note(w)
    nid = await _w_create_note(w, target)
    p = await _part0(w, nid)
    async with tenant_session(str(w.org), str(w.user)) as s:
        await np.update_part(
            s,
            org_id=w.org,
            actor_id=w.user,
            part_id=p.id,
            expected_version=p.version,
            body="no citation any more",
        )
    assert await _backlinks(w, "note", target) == []


# ---------------------------------------------------------------- resolution


async def test_every_syntax_and_target_kind() -> None:
    w = await _ws()
    note = await _note(w)
    task = await _task(w, title="Cited task")
    async with tenant_session(str(w.org), str(w.user)) as s:
        from mycelium_core.services import taxonomy

        tag = await taxonomy.create_project(s, org_id=w.org, actor_id=w.user, name="cited")
        tag_id = tag.id
    body = (
        f"[n](@note:{note}) [t](@task:{task}) [g](@tag:{tag_id}) "
        f"`{str(task)[:8]}` and bare {str(note)[:8]}."
    )
    citing = await _note(w, text=body, title="Citing")
    by_note = await _backlinks(w, "note", note)
    by_task = await _backlinks(w, "task", task)
    by_tag = await _backlinks(w, "tag", tag_id)
    assert sorted(b.syntax for b in by_note) == ["bare", "mention"]
    assert sorted(b.syntax for b in by_task) == ["chip", "mention"]
    assert [b.syntax for b in by_tag] == ["mention"]
    assert {b.note_id for b in by_note + by_task + by_tag} == {citing}


async def test_a_rename_keeps_the_backlink() -> None:
    w = await _ws()
    target = await _note(w, title="Old name")
    citing = await _w_create_note(w, target)
    async with tenant_session(str(w.org), str(w.user)) as s:
        n = await nt.get_note(s, org_id=w.org, note_id=target)
        await nt.update_note(
            s,
            org_id=w.org,
            actor_id=w.user,
            note_id=target,
            expected_version=n.version,
            title="New name",
        )
    assert [b.note_id for b in await _backlinks(w, "note", target)] == [citing]


async def test_an_unresolving_bare_code_or_short_chip_is_not_a_reference() -> None:
    w = await _ws()
    nid = await _note(w, text="sha deadbeef and `cafe` and `1234`", title="Prose")
    async with tenant_session(str(w.org), str(w.user)) as s:
        rows = (await s.execute(select(BodyReference))).scalars().all()
        assert [r for r in rows if r.org_id == w.org] == []
        src = (
            await s.execute(
                select(BodyReferenceSource)
                .join(NotePart, NotePart.id == BodyReferenceSource.note_part_id)
                .where(NotePart.note_id == nid)
            )
        ).scalar_one()
        assert src.body_sha256 == br.body_sha256("sha deadbeef and `cafe` and `1234`")


async def test_a_long_chip_that_names_nothing_is_dangling() -> None:
    w = await _ws()
    nid = await _note(w, text="see `0000beef`", title="Prose")
    async with tenant_session(str(w.org), str(w.user)) as s:
        rows = (
            (await s.execute(select(BodyReference).where(BodyReference.org_id == w.org)))
            .scalars()
            .all()
        )
    assert [(r.syntax, r.status, r.target_id) for r in rows] == [("chip", "dangling", None)]
    assert nid


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("0000beef", True),
        ("91cf6aaa-1111", True),
        ("91cf6aaa-1111-4222", True),
        ("91cf6aaa-1111-4222-8333", True),
        ("91cf6aaa-1111-4222-8333-444455556666", True),
        # Numbers in backticks: a size, a phone number, a VAT number.
        ("10485760", False),
        ("00391234567890", False),
        ("12345678901", False),
        # Git shas and other hex runs no code is written as.
        ("1ba4dcaaa", False),
        ("8b8b5602c0ff", False),
        ("8b8b5602c0ffee00", False),
        ("8b8b5602c0ffee008b8b5602c0ffee00", False),
        # A UUID, but not one a row id can be (version 3).
        ("1668ec92-db12-3c65-86fb-e3e766d1ec30", False),
        ("91cf6aaa-11", False),
    ],
)
def test_which_unresolved_chips_are_dangling(token: str, expected: bool) -> None:
    assert br.chip_could_name_an_entity(token) is expected


async def _dangling_rows(w: W) -> list[tuple[str, str]]:
    async with tenant_session(str(w.org), str(w.user)) as s:
        rows = (
            (await s.execute(select(BodyReference).where(BodyReference.org_id == w.org)))
            .scalars()
            .all()
        )
    return sorted((r.token, r.status) for r in rows)


async def test_recheck_applies_the_current_rule_to_bodies_nobody_edits() -> None:
    """A dangling chip recorded under an earlier rule is removed when the
    rule no longer counts it, and one whose target now exists resolves;
    the body is not edited and stays projected."""
    w = await _ws()
    code = uuid.uuid4().hex[:7] + "a"
    body = f"size `10485760`, see `{code}` and `{uuid.uuid4().hex[:7]}b`"
    nid = await _note(w, text=body, title="Prose")
    async with tenant_session(str(w.org), str(w.user)) as s:
        src = (
            await s.execute(
                select(BodyReferenceSource)
                .join(NotePart, NotePart.id == BodyReferenceSource.note_part_id)
                .where(NotePart.note_id == nid)
            )
        ).scalar_one()
        # What the earlier rule (eight hex digits) recorded.
        s.add(
            BodyReference(
                org_id=w.org,
                source_id=src.id,
                syntax="chip",
                token="10485760",
                ordinal=0,
                status="dangling",
            )
        )
    before = await _dangling_rows(w)
    assert [t for t, st in before if st == "dangling"].count("10485760") == 1
    target = uuid.UUID(code + str(uuid.uuid4())[8:])
    await _note_with_id(w, nid, target, "Now exists")
    async with tenant_session(str(w.org), str(w.user)) as s:
        changed, after = await br.recheck_dangling(s, org_id=w.org, limit=10)
        assert (changed, after) == (2, src.id)
        assert await br.recheck_dangling(s, org_id=w.org, after=after) == (0, None)
    after_rows = dict(await _dangling_rows(w))
    assert "10485760" not in after_rows
    assert after_rows[code] == "resolved"
    assert sorted(after_rows.values()) == ["dangling", "resolved"]
    async with tenant_session(str(w.org), str(w.user)) as s:
        assert await br.count_drift(s, org_id=w.org) == 0


async def _note_with_id(w: W, like: uuid.UUID, note_id: uuid.UUID, title: str) -> None:
    """A bare note row with a chosen id, filed like ``like``: the only way to
    reach, on demand, the prefix collision a real workspace reaches by
    chance."""
    async with tenant_session(str(w.org), str(w.user)) as s:
        s.add(Note(id=note_id, org_id=w.org, kind=NoteKind.text, title=title))
        await s.flush()
        tags = (await s.execute(select(NoteTag.tag_id).where(NoteTag.note_id == like))).scalars()
        for tag_id in list(tags):
            s.add(NoteTag(org_id=w.org, note_id=note_id, tag_id=tag_id))


async def _colliding_notes(w: W) -> tuple[uuid.UUID, uuid.UUID, str]:
    a = await _note(w, title="A")
    prefix = str(a)[:8]
    b = uuid.UUID(prefix + str(uuid.uuid4())[8:])
    await _note_with_id(w, a, b, "B")
    return a, b, prefix


async def test_an_ambiguous_chip_stays_ambiguous_until_a_person_chooses() -> None:
    w = await _ws()
    a, b, prefix = await _colliding_notes(w)
    citing = await _note(w, text=f"which `{prefix}`?", title="Citing")
    for target in (a, b):
        links = await _backlinks(w, "note", target)
        assert [(x.note_id, x.status) for x in links] == [(citing, "ambiguous")]
    ref_id = (await _backlinks(w, "note", a))[0].reference_id
    async with tenant_session(str(w.org), str(w.user)) as s:
        await br.choose_target(
            s,
            org_id=w.org,
            actor_id=w.user,
            reference_id=ref_id,
            target_kind="note",
            target_id=b,
        )
    assert await _backlinks(w, "note", a) == []
    assert [(x.note_id, x.status) for x in await _backlinks(w, "note", b)] == [(citing, "resolved")]
    # The choice survives an edit elsewhere in the same body.
    p = await _part0(w, citing)
    async with tenant_session(str(w.org), str(w.user)) as s:
        await np.update_part(
            s,
            org_id=w.org,
            actor_id=w.user,
            part_id=p.id,
            expected_version=p.version,
            body=f"Edited before. which `{prefix}`?",
        )
    assert [x.status for x in await _backlinks(w, "note", b)] == ["resolved"]


async def test_a_later_collision_does_not_move_a_resolved_reference() -> None:
    w = await _ws()
    a = await _note(w, title="A")
    prefix = str(a)[:8]
    citing = await _note(w, text=f"cites `{prefix}`", title="Citing")
    await _note_with_id(w, a, uuid.UUID(prefix + str(uuid.uuid4())[8:]), "B")
    p = await _part0(w, citing)
    async with tenant_session(str(w.org), str(w.user)) as s:
        await np.update_part(
            s,
            org_id=w.org,
            actor_id=w.user,
            part_id=p.id,
            expected_version=p.version,
            body=f"New words. cites `{prefix}`",
        )
    assert [(x.note_id, x.status) for x in await _backlinks(w, "note", a)] == [(citing, "resolved")]


async def test_choose_target_refuses_what_it_should() -> None:
    w = await _ws()
    target = await _note(w)
    await _w_create_note(w, target)
    ref_id = (await _backlinks(w, "note", target))[0].reference_id
    other = await _note(w, title="Other")
    async with tenant_session(str(w.org), str(w.user)) as s:
        with pytest.raises(ConflictError):
            await br.choose_target(
                s,
                org_id=w.org,
                actor_id=w.user,
                reference_id=ref_id,
                target_kind="note",
                target_id=other,
            )
    a, _b, prefix = await _colliding_notes(w)
    await _note(w, text=f"`{prefix}`", title="Citing")
    amb = (await _backlinks(w, "note", a))[0].reference_id
    async with tenant_session(str(w.org), str(w.user)) as s:
        with pytest.raises(UnprocessableError):
            await br.choose_target(
                s,
                org_id=w.org,
                actor_id=w.user,
                reference_id=amb,
                target_kind="note",
                target_id=other,
            )


# ---------------------------------------------------------------- reading


async def test_backlinks_exclude_sources_in_the_bin_and_label_archived() -> None:
    w = await _ws()
    target = await _note(w)
    trashed = await _w_create_note(w, target)
    archived = await _w_create_note(w, target)
    async with tenant_session(str(w.org), str(w.user)) as s:
        t = await nt.get_note(s, org_id=w.org, note_id=trashed)
        await nt.soft_delete_note(
            s, org_id=w.org, actor_id=w.user, note_id=trashed, expected_version=t.version
        )
        n = await nt.get_note(s, org_id=w.org, note_id=archived)
        await nt.archive_note(
            s, org_id=w.org, actor_id=w.user, note_id=archived, expected_version=n.version
        )
    links = await _backlinks(w, "note", target)
    assert [(b.note_id, b.source_archived) for b in links] == [(archived, True)]


async def test_backlinks_carry_the_sentence_and_typed_overlap() -> None:
    w = await _ws()
    target = await _note(w)
    citing = await _note(
        w, text=f"Intro. We follow {_chip(target)} closely. Outro.", title="Citing"
    )
    async with tenant_session(str(w.org), str(w.user)) as s:
        from mycelium_core.services import note_links

        await note_links.link_notes(
            s,
            org_id=w.org,
            actor_id=w.user,
            parent_note_id=citing,
            child_note_id=target,
            kind="related",
        )
    (b,) = await _backlinks(w, "note", target)
    assert (b.quote_prefix, b.quote_exact, b.quote_suffix) == (
        "We follow ",
        str(target)[:8],
        " closely.",
    )
    assert b.also_typed is True


async def test_a_note_citing_itself_is_not_its_own_backlink() -> None:
    w = await _ws()
    nid = await _note(w, text="placeholder", title="Self")
    p = await _part0(w, nid)
    async with tenant_session(str(w.org), str(w.user)) as s:
        await np.update_part(
            s,
            org_id=w.org,
            actor_id=w.user,
            part_id=p.id,
            expected_version=p.version,
            body=f"me {_chip(nid)}",
        )
    assert await _backlinks(w, "note", nid) == []


async def test_index_scope_none_is_still_projected() -> None:
    w = await _ws()
    target = await _note(w)
    async with tenant_session(str(w.org), str(w.user)) as s:
        from mycelium_core.models.index_scope import IndexScope

        n = await nt.create_note(
            s,
            org_id=w.org,
            actor_id=w.user,
            kind=NoteKind.text,
            title="Private",
            text=f"cites {_chip(target)}",
            index_scope=IndexScope.none,
        )
        nid = n.id
    assert [b.note_id for b in await _backlinks(w, "note", target)] == [nid]


# ---------------------------------------------------------------- drift


async def test_drift_is_counted_and_repaired() -> None:
    w = await _ws()
    target = await _note(w)
    nid = await _note(w, text="nothing", title="Citing")
    async with tenant_session(str(w.org), str(w.user)) as s:
        assert await br.count_drift(s, org_id=w.org) == 0
        # A write that bypasses every mark: the failure the verifier exists for.
        await s.execute(
            update(NotePart).where(NotePart.note_id == nid).values(body=f"silently {_chip(target)}")
        )
    s_info_cleared = await _backlinks(w, "note", target)
    assert s_info_cleared == []
    async with tenant_session(str(w.org), str(w.user)) as s:
        assert await br.count_drift(s, org_id=w.org) == 1
        report = await br.repair_drift(s, org_id=w.org)
        assert (report.parts, report.tasks) == (1, 0)
    links = await _backlinks(w, "note", target)
    assert [(b.note_id, b.first_seen_at) for b in links] == [(nid, None)]
    async with tenant_session(str(w.org), str(w.user)) as s:
        assert await br.count_drift(s, org_id=w.org) == 0


async def test_first_seen_at_is_the_commit_for_live_writes() -> None:
    w = await _ws()
    target = await _note(w)
    await _w_create_note(w, target)
    (b,) = await _backlinks(w, "note", target)
    assert b.first_seen_at is not None


# ---------------------------------------------------------------- backfill


async def test_backfill_projects_and_reports_once() -> None:
    from mycelium_core import backfill_body_references as bf

    w = await _ws()
    target = await _note(w)
    nid = await _note(w, text="nothing", title="Citing")
    async with tenant_session(str(w.org), str(w.user)) as s:
        await s.execute(
            update(NotePart)
            .where(NotePart.note_id == nid)
            .values(body=f"{_chip(target)} and `ffffffff`")
        )
    first = await bf.backfill_org(w.org, w.user)
    assert (first.projected_parts, first.projected_tasks) == (1, 0)
    assert first.by_syntax_status == {("chip", "resolved"): 1, ("chip", "dangling"): 1}
    assert len(first.unresolved) == 1 and "ffffffff" in first.unresolved[0]
    second = await bf.backfill_org(w.org, w.user)
    assert (second.projected_parts, second.projected_tasks) == (0, 0)
    assert second.by_syntax_status == first.by_syntax_status
    assert "occurrences: 2" in bf.render([second])


async def test_worker_repair_tick_reaches_every_workspace() -> None:
    from mycelium_worker import body_references as worker_refs

    w = await _ws()
    target = await _note(w)
    nid = await _note(w, text="nothing", title="Citing")
    async with tenant_session(str(w.org), str(w.user)) as s:
        await s.execute(update(NotePart).where(NotePart.note_id == nid).values(body=_chip(target)))
    assert await worker_refs.run_once() >= 1
    assert [b.note_id for b in await _backlinks(w, "note", target)] == [nid]


async def test_two_transactions_projecting_one_body_both_commit() -> None:
    """A person's edit and the repair sweep can project the same body at
    once. The second must wait for the first and then find nothing to do,
    never fail on the unique source index: that failure would roll back the
    person's edit."""
    import asyncio

    w = await _ws()
    target = await _note(w)
    nid = await _note(w, text="nothing", title="Citing")
    p = await _part0(w, nid)
    async with tenant_session(str(w.org), str(w.user)) as s:
        await s.execute(
            update(NotePart).where(NotePart.id == p.id).values(body=f"race {_chip(target)}")
        )
        await s.execute(
            BodyReferenceSource.__table__.delete().where(BodyReferenceSource.note_part_id == p.id)
        )
    first_holds = asyncio.Event()
    release = asyncio.Event()

    async def first() -> None:
        async with tenant_session(str(w.org), str(w.user)) as s:
            await br.project_part(s, part_id=p.id, first_seen_at=None)
            first_holds.set()
            await release.wait()

    async def second() -> None:
        await first_holds.wait()
        async with tenant_session(str(w.org), str(w.user)) as s:
            await br.project_part(s, part_id=p.id, first_seen_at=None)

    t1 = asyncio.create_task(first())
    t2 = asyncio.create_task(second())
    await first_holds.wait()
    await asyncio.sleep(0.5)
    assert not t2.done()  # waiting on the first transaction, not failed
    release.set()
    await asyncio.gather(t1, t2)
    assert [b.note_id for b in await _backlinks(w, "note", target)] == [nid]


# ---------------------------------------------------------------- graph (decision 6)


async def _weights(w: W) -> dict[tuple[uuid.UUID, uuid.UUID], float]:
    from mycelium_core.services import graph

    async with tenant_session(str(w.org), str(w.user)) as s:
        edges = await graph.compute_note_edge_weights(s, org_id=w.org)
    return {(e.src, e.dst): e.weight for e in edges}


async def _local(w: W, note_id: uuid.UUID) -> dict[uuid.UUID, float]:
    from mycelium_core.services import graph_local

    async with tenant_session(str(w.org), str(w.user)) as s:
        return await graph_local.local_edges(s, org_id=w.org, note_id=note_id)


async def test_written_references_enter_the_graph_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mycelium_core.config import get_settings
    from mycelium_core.models.index_scope import IndexScope
    from mycelium_core.services import graph

    w = await _ws()
    target = await _note(w)
    citing = await _w_create_note(w, target)
    async with tenant_session(str(w.org), str(w.user)) as s:
        hidden = await nt.create_note(
            s,
            org_id=w.org,
            actor_id=w.user,
            kind=NoteKind.text,
            title="Private",
            text=f"cites {_chip(target)}",
            index_scope=IndexScope.none,
        )
        hidden_id = hidden.id
    pair = graph._pair_key(citing, target)  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setattr(get_settings(), "graph_written_references_enabled", False)
    assert pair not in await _weights(w)
    assert citing not in await _local(w, target)

    monkeypatch.setattr(get_settings(), "graph_written_references_enabled", True)
    weights = await _weights(w)
    assert weights[pair] == pytest.approx(0.45)
    # Same weight from the bounded builder: the parity contract.
    assert (await _local(w, target))[citing] == pytest.approx(weights[pair])
    # A source kept out of automatic indexing is a backlink, not an edge.
    assert graph._pair_key(hidden_id, target) not in weights  # pyright: ignore[reportPrivateUsage]
    assert hidden_id in {b.note_id for b in await _backlinks(w, "note", target)}


async def test_a_written_reference_from_the_bin_is_not_an_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mycelium_core.config import get_settings
    from mycelium_core.services import graph

    monkeypatch.setattr(get_settings(), "graph_written_references_enabled", True)
    w = await _ws()
    target = await _note(w)
    citing = await _w_create_note(w, target)
    async with tenant_session(str(w.org), str(w.user)) as s:
        n = await nt.get_note(s, org_id=w.org, note_id=citing)
        await nt.soft_delete_note(
            s, org_id=w.org, actor_id=w.user, note_id=citing, expected_version=n.version
        )
    assert graph._pair_key(citing, target) not in await _weights(w)  # pyright: ignore[reportPrivateUsage]
    assert await _local(w, target) == {}
