"""Service-layer patch helpers: the gate->apply->setter wiring for the
three text targets, and the PatchError -> domain-error translation
(``apply_patch_text``).

The pure translation cases need no DB; the three ``apply_patch_*`` helpers
run against the dev DB (signup -> tenant_session) like
``test_capability_tokens.py``, confirming each loads the live body, gates
on the sha256, applies the diff, and bumps the row version through the
existing setter.
"""

from __future__ import annotations

import difflib
import uuid

import pytest
from sqlalchemy import select

from flow_core.db import admin_session, tenant_session
from flow_core.errors import ConflictError, DomainError, UnprocessableError
from flow_core.i18n import MessageCode
from flow_core.models.annotation import Annotation
from flow_core.models.note import NoteKind
from flow_core.models.note_part import NotePart
from flow_core.models.task import Task
from flow_core.services import annotations as ann_svc
from flow_core.services import note_parts as parts_svc
from flow_core.services import notes as notes_svc
from flow_core.services import tasks as tasks_svc
from flow_core.services import text_patch
from flow_core.services.auth import signup


def _udiff(a: str, b: str) -> str:
    return "".join(
        difflib.unified_diff(
            a.splitlines(keepends=True), b.splitlines(keepends=True), lineterm="\n"
        )
    )


# --- pure translation (no DB) -------------------------------------------


def test_apply_patch_text_happy() -> None:
    base = "a\nb\nc\n"
    new = "a\nB\nc\n"
    out = text_patch.apply_patch_text(
        base, _udiff(base, new), expected_sha256=text_patch.body_sha256(base)
    )
    assert out == new


def test_apply_patch_text_sha_drift_is_conflict_stale() -> None:
    base = "a\nb\n"
    with pytest.raises(ConflictError) as ei:
        text_patch.apply_patch_text(
            base, _udiff(base, "a\nB\n"), expected_sha256=text_patch.body_sha256("other\n")
        )
    assert ei.value.code == MessageCode.PATCH_STALE


def test_apply_patch_text_malformed_is_unprocessable() -> None:
    base = "a\n"
    with pytest.raises(UnprocessableError) as ei:
        text_patch.apply_patch_text(
            base, "not a diff", expected_sha256=text_patch.body_sha256(base)
        )
    assert ei.value.code == MessageCode.PATCH_MALFORMED


def test_apply_patch_text_non_applying_is_unprocessable() -> None:
    base = "a\nb\nc\n"
    patch = _udiff(base, "a\nB\nc\n").replace(" a\n", " X\n", 1)
    with pytest.raises(UnprocessableError) as ei:
        text_patch.apply_patch_text(base, patch, expected_sha256=text_patch.body_sha256(base))
    assert ei.value.code == MessageCode.PATCH_DOES_NOT_APPLY


def test_apply_patch_text_oversize_is_body_limit() -> None:
    base = ""
    big = "x" * 200 + "\n"
    with pytest.raises(DomainError) as ei:
        text_patch.apply_patch_text(
            base,
            _udiff(base, big),
            expected_sha256=text_patch.body_sha256(base),
            max_result_bytes=10,
        )
    assert ei.value.code == MessageCode.BODY_LIMIT_EXCEEDED


# --- DB-backed helpers ---------------------------------------------------


async def _signup() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="PATCHSVC",
        )
    return r.org_id, r.user_id


async def test_apply_patch_to_part_happy() -> None:
    org, user = await _signup()
    base = "line one\nline two\n"
    new = "line one edited\nline two\nline three\n"
    async with tenant_session(str(org), str(user)) as s:
        note = await notes_svc.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, text=base
        )
        # Column select (not the ORM entity) so the reload reflects the
        # core UPDATE inside the setter, not a stale identity-map row.
        part_id, part_version = (
            await s.execute(
                select(NotePart.id, NotePart.version).where(NotePart.note_id == note.id)
            )
        ).first()
        v = await parts_svc.apply_patch_to_part(
            s,
            org_id=org,
            actor_id=user,
            part_id=part_id,
            expected_version=part_version,
            patch=_udiff(base, new),
            base_sha256=text_patch.body_sha256(base),
        )
        assert v == part_version + 1
        body = (await s.execute(select(NotePart.body).where(NotePart.id == part_id))).scalar_one()
        assert body == new


async def test_apply_patch_to_part_version_drift_conflicts() -> None:
    org, user = await _signup()
    base = "a\nb\n"
    async with tenant_session(str(org), str(user)) as s:
        note = await notes_svc.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, text=base
        )
        part_id = (
            await s.execute(select(NotePart.id).where(NotePart.note_id == note.id))
        ).scalar_one()
        # A stale expected_version is the optimistic-concurrency 409, with
        # the correct base sha256 (so the version gate, not the base gate,
        # is what fires).
        with pytest.raises(ConflictError) as ei:
            await parts_svc.apply_patch_to_part(
                s,
                org_id=org,
                actor_id=user,
                part_id=part_id,
                expected_version=999,
                patch=_udiff(base, "a\nB\n"),
                base_sha256=text_patch.body_sha256(base),
            )
        assert ei.value.code == MessageCode.CONFLICT_STALE_VERSION


async def test_apply_patch_to_description_happy() -> None:
    org, user = await _signup()
    base = "intro\nbody\n"
    new = "intro\nbody edited\ntail\n"
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="patch me", description=base
        )
        task_id, task_version = task.id, task.version
        v = await tasks_svc.apply_patch_to_description(
            s,
            org_id=org,
            actor_id=user,
            task_id=task_id,
            expected_version=task_version,
            patch=_udiff(base, new),
            base_sha256=text_patch.body_sha256(base),
        )
        assert v == task_version + 1
        desc = (await s.execute(select(Task.description).where(Task.id == task_id))).scalar_one()
        assert desc == new


async def test_apply_patch_to_annotation_body_happy() -> None:
    org, user = await _signup()
    base = "first note\nsecond note\n"
    new = "first note edited\nsecond note\nthird\n"
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="host")
        ann = await ann_svc.create_comment(
            s,
            org_id=org,
            actor_id=user,
            doc_kind="task_description",
            doc_id=task.id,
            body=base,
        )
        ann_id, ann_version = ann.id, ann.version
        v = await ann_svc.apply_patch_to_body(
            s,
            org_id=org,
            actor_id=user,
            annotation_id=ann_id,
            expected_version=ann_version,
            patch=_udiff(base, new),
            base_sha256=text_patch.body_sha256(base),
        )
        assert v == ann_version + 1
        body = (
            await s.execute(select(Annotation.body).where(Annotation.id == ann_id))
        ).scalar_one()
        assert body == new
