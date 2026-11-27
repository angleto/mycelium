"""No MCP read of note text is larger than a client takes, and none loses a byte.

Reported from the field on 2026-11-25: three notes of 95 to 137 kB could not
be read at all from Claude Code. Each was a single part, so reading by parts
(the escape ADR-0064 pointed to) returned the same 120 kB, and the client
refused the result. ``garden_review_read`` budgeted its sources but not the
proposal or the original it revises.

The contract pinned here:

- every note text an MCP answer carries is spent from one budget of
  ``READ_BUDGET_BYTES``, in bytes, whatever the note's size or alphabet;
- a text cut to fit says so (``body_truncated``, the whole ``body_bytes``)
  and gives a ``next_cursor``; the windows read from it end at line ends and
  join to the body exactly;
- a cursor read after the text changed is ``concurrency.stale_version`` with
  the current version, never a splice of two revisions;
- every READ operation bound as an MCP tool that returns free text is
  windowed, says why its text is short, or names the task that will bound
  it, so a new one cannot ship unbounded the way the note reads did.

Observed against the sources of 98f5fe4e (2026-11-27), with the window tests
left out since their module did not exist: four of the five MCP tests
failed. ``get_note`` on a 147 kB single-part note answered 150,593 bytes and
``list_note_parts`` 150,261, a 92 kB proposal came back whole from
``garden_review_read`` at 90,254, and a part had no ``next_cursor`` to
continue from. The fifth, the registry walk, passed there too: it is a fence
for the next operation, not a measure of this one.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from mycelium_core import operations as ops
from mycelium_core import text_window
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import ConflictError, UnprocessableError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.note import NoteKind
from mycelium_core.services import notes as nt
from mycelium_core.services.auth import signup
from mycelium_core.services.text_patch import body_sha256
from mycelium_mcp.gateway import execute_tool
from mycelium_mcp.server import _PRINCIPAL

BUDGET = text_window.READ_BUDGET_BYTES

#: What an answer carries besides its texts: ids, versions, digests, tags.
#: Measured at about 1.1 kB for a one-part note; the bound is on the texts,
#: so this only has to be generous and constant.
_ENVELOPE = 4_000


def _size(payload: Any) -> int:
    """Bytes as the gateway sends them: compact JSON, UTF-8, not escaped."""
    return len(json.dumps(payload, separators=(",", ":"), default=str, ensure_ascii=False).encode())


def _recipes(n_bytes: int) -> str:
    """A long single-part note shaped like the reported ones: markdown
    lines, accented Italian, one very long line in the middle."""
    lines = [f"- ricetta {i:04d}: farina, uova, però niente burro" for i in range(n_bytes // 50)]
    lines.insert(len(lines) // 2, "x" * 30_000)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        _recipes(60_000),
        "è" * 40_000,  # two bytes each, no newline
        "🍄" * 20_000,  # four bytes each, no newline
        "a\n" * 30_000,
        "",
    ],
)
def test_windows_fit_the_budget_end_at_lines_and_join_exactly(text: str) -> None:
    pieces, start = [], 0
    while True:
        w = text_window.window(text, start=start, budget=BUDGET)
        assert w.bytes <= BUDGET
        assert w.total_bytes == len(text.encode("utf-8"))
        if w.next_start is not None and "\n" in w.text:
            assert w.text.endswith("\n"), "a window holding a line end cuts after it"
        pieces.append(w.text)
        if w.next_start is None:
            break
        assert w.next_start > start, "every window makes progress"
        start = w.next_start
    assert "".join(pieces) == text


def test_a_spent_budget_still_says_the_text_is_there() -> None:
    w = text_window.window("abc", budget=0)
    assert (w.text, w.truncated, w.next_start, w.total_bytes) == ("", True, 0, 3)


def test_shares_keep_short_texts_whole_and_split_the_rest() -> None:
    assert text_window.shares([100, 50_000, 80_000], 24_000) == [100, 11_950, 11_950]
    assert text_window.shares([10, 20], 24_000) == [10, 20]
    assert sum(text_window.shares([30_000] * 7, 24_000)) <= 24_000


def test_a_cursor_from_another_text_is_a_stale_read_and_a_forged_one_is_refused() -> None:
    w = text_window.window("riga\n" * 10_000, budget=BUDGET)
    token = text_window.cursor(w, body_sha256("riga\n" * 10_000))
    assert token is not None
    assert text_window.resume(token, "riga\n" * 10_000, version=3, operation="t") == w.end
    with pytest.raises(ConflictError) as stale:
        text_window.resume(token, "riga\n" * 10_001, version=4, operation="t")
    assert stale.value.code is MessageCode.CONFLICT_STALE_VERSION
    assert stale.value.params == {"current_version": 4}
    with pytest.raises(UnprocessableError):
        text_window.resume("not-a-cursor", "x", version=1, operation="t")


# --------------------------------------------------------------------------
# The MCP reads
# --------------------------------------------------------------------------


@asynccontextmanager
async def _principal() -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="WINDOWS",
        )
    reset = _PRINCIPAL.set((r.user_id, r.org_id, None))
    try:
        yield r.org_id, r.user_id
    finally:
        _PRINCIPAL.reset(reset)


async def _note(org: uuid.UUID, user: uuid.UUID, body: str, **fields: Any) -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="Ricettario", text=body
        )
        for k, v in fields.items():
            setattr(note, k, v)
        await s.flush()
        return note.id


async def _call(name: str, **arguments: Any) -> Any:
    out = await execute_tool(name=name, arguments=arguments)
    assert not (isinstance(out, dict) and "error" in out), out
    return out


async def _read_part(part_id: str, first: dict[str, Any]) -> str:
    """The body of a part, read window by window from the first one."""
    pieces, cursor = [first.get("body", "")], first.get("next_cursor")
    while cursor is not None:
        got = await _call("get_note_part", part_id=part_id, cursor=cursor)
        assert _size(got) <= BUDGET + _ENVELOPE, _size(got)
        pieces.append(got.get("body", ""))
        cursor = got.get("next_cursor")
    return "".join(pieces)


async def test_a_single_part_note_of_147_kb_is_read_whole_through_windows() -> None:
    body = _recipes(120_000)
    async with _principal() as (org, user):
        nid = await _note(org, user, body)
        got = await _call("get_note", note_id=str(nid))
        assert _size(got) <= BUDGET + _ENVELOPE, _size(got)
        (part,) = got["parts"]
        assert part["body_truncated"] is True
        assert part["body_bytes"] == len(body.encode("utf-8"))
        assert part["body_sha256"] == body_sha256(body), "the digest stays the whole body's"
        assert await _read_part(part["id"], part) == body

        direct = await _call("get_note_part", part_id=part["id"])
        assert _size(direct) <= BUDGET + _ENVELOPE
        assert await _read_part(part["id"], direct) == body


async def test_the_other_note_reads_share_the_budget_too() -> None:
    body = _recipes(120_000)
    async with _principal() as (org, user):
        nid = await _note(org, user, body)
        parts = await _call("list_note_parts", note_id=str(nid), include_body=True)
        assert _size(parts) <= BUDGET + _ENVELOPE, _size(parts)
        both = await _call("get_note", note_id=str(nid), include_transcript=True)
        assert _size(both) <= BUDGET + _ENVELOPE, _size(both)
        assert both["transcript_truncated"] is True
        page = await _call("list_notes", include_transcript=True)
        assert _size(page) <= BUDGET + _ENVELOPE * 2, _size(page)

        task = await _call("create_task", title="with a long work note")
        note = await _call("get_or_create_task_note", task_id=task["id"])
        wpart = await _call("add_note_part", note_id=note["id"], body=body)
        work = await _call("get_or_create_task_note", task_id=task["id"])
        assert _size(work) <= BUDGET + _ENVELOPE, _size(work)
        assert await _read_part(wpart["id"], work["parts"][0]) == body


async def test_a_cursor_read_after_an_edit_is_stale_not_spliced() -> None:
    body = _recipes(60_000)
    async with _principal() as (org, user):
        nid = await _note(org, user, body)
        (part,) = (await _call("get_note", note_id=str(nid)))["parts"]
        edited = await _call(
            "replace_in_note_part",
            part_id=part["id"],
            find="ricetta 0000",
            replace="ricetta zero",
            expected_version=part["version"],
        )
        stale = await execute_tool(
            name="get_note_part",
            arguments={"part_id": part["id"], "cursor": part["next_cursor"]},
        )
    assert stale["error"]["code"] == "concurrency.stale_version", stale
    assert stale["error"]["params"]["current_version"] == edited["version"]


async def test_a_long_proposal_is_cut_and_read_on_through_the_review_tools() -> None:
    """The proposal itself counts against the budget, and since the note
    tools do not open a proposal, the review tool continues it."""
    body = _recipes(60_000)
    async with _principal() as (org, user):
        nid = await _note(org, user, body, review_state="proposed", origin_model_id="small")
        reading = await _call("garden_review_read", note_id=str(nid))
        assert _size(reading) <= BUDGET + _ENVELOPE, _size(reading)
        assert reading["body_truncated"] is True
        pieces, cursor = [reading["body"]], reading["next_cursor"]
        while cursor is not None:
            got = await _call(
                "garden_review_read_source",
                note_id=str(nid),
                source_note_id=str(nid),
                cursor=cursor,
            )
            assert _size(got) <= BUDGET + _ENVELOPE
            pieces.append(got["text"])
            cursor = got.get("next_cursor")
        assert "".join(pieces) == body

        whole = await execute_tool(
            name="garden_review_read", arguments={"note_id": str(nid), "full_text": True}
        )
    assert "error" in whole, "the whole reading is the app's, not offered over MCP"


# --------------------------------------------------------------------------
# Every free-text read is accounted for
# --------------------------------------------------------------------------

#: Field names that carry free text a person or a model wrote.
_FREE_TEXT = {"body", "transcript", "text", "description", "summary", "content"}

#: Each MCP-bound READ operation whose result has an unbounded free-text
#: field, with what bounds it on this surface. A new one fails the test
#: below until it is windowed or its text is shown to be short.
_ACCOUNTED: dict[str, str] = {
    "notes.get": "windowed: read_budget.note, continued by get_note_part",
    "notes.parts.list": "windowed: read_budget.parts, continued by get_note_part",
    "notes.list": "windowed: read_budget.note_rows on the transcripts",
    "garden.review.read": "windowed: text_window.shares, continued by read_source",
    "garden.review.read_source": "windowed: one window of READ_BUDGET_BYTES",
    # Open, not bounded: every task's whole description and every comment.
    # Tracked as task 0fa73a5c with the hand-written task tools (get_task's
    # full_description, task_pull's brief), which this registry walk cannot see.
    "tasks.work_digest": "open: task 0fa73a5c",
}


def _free_text_fields(tp: Any, seen: frozenset[type] = frozenset()) -> set[str]:
    import types
    import typing

    from pydantic import BaseModel

    origin = typing.get_origin(tp)
    if origin is not None:
        return set().union(*(_free_text_fields(a, seen) for a in typing.get_args(tp)))
    if not (isinstance(tp, type) and issubclass(tp, BaseModel)) or tp in seen:
        return set()
    out: set[str] = set()
    for name, field in tp.model_fields.items():
        ann = field.annotation
        union = typing.get_origin(ann) in (typing.Union, types.UnionType)
        is_str = ann is str or (union and str in typing.get_args(ann))
        capped = any(getattr(m, "max_length", None) for m in field.metadata)
        if is_str and name in _FREE_TEXT and not capped:
            out.add(name)
        else:
            out |= _free_text_fields(ann, seen | {tp})
    return out


def test_every_free_text_read_on_mcp_is_windowed_or_says_why_not() -> None:
    import mycelium_mcp.server  # noqa: F401  (binds every tool)
    from mycelium_mcp.op_tools import TOOL_OPERATIONS

    unaccounted = sorted(
        op_id
        for op_id in set(TOOL_OPERATIONS.values())
        if ops.get(op_id).kind is ops.Kind.READ
        and _free_text_fields(ops.get(op_id).result)
        and op_id not in _ACCOUNTED
    )
    assert not unaccounted, (
        f"{unaccounted} return free text over MCP with nothing bounding it: window it "
        "(mycelium_mcp.read_budget) or record here why it is short"
    )
