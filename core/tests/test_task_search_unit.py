"""Pure-function unit tests for the task-search rendering / hashing.

The DB-driven paths (listeners, /search endpoint, embedder fallback)
are exercised by ``api/tests/test_search_unified.py``; this file
covers the small synchronous helpers that don't need a session.
"""

from __future__ import annotations

import datetime as dt
import uuid

from mycelium_core.models.task import Necessity, Task
from mycelium_core.models.task_checklist_item import TaskChecklistItem
from mycelium_core.services.task_search import (
    UnifiedHit,
    _fuse_branches,
    content_hash,
    render_task_for_search,
)


def _task(
    *,
    title: str = "T",
    description: str | None = None,
) -> Task:
    """Build a minimally-valid Task for rendering (no session, no PK gen)."""
    now = dt.datetime.now(tz=dt.UTC)
    t = Task(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        title=title,
        description=description,
        state_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        priority=3,
        importance=4,
        urgency=4,
        necessity=Necessity.should,
        created_at=now,
        updated_at=now,
        version=1,
    )
    return t


def _item(text: str, *, position: int = 0, done: bool = False) -> TaskChecklistItem:
    now = dt.datetime.now(tz=dt.UTC)
    return TaskChecklistItem(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        text=text,
        done=done,
        position=position,
        created_at=now,
        updated_at=now,
        version=1,
    )


def test_render_title_only() -> None:
    t = _task(title="Plan the migration")
    assert render_task_for_search(t, []) == "Plan the migration"


def test_render_title_and_description() -> None:
    t = _task(title="A", description="bbb")
    assert render_task_for_search(t, []) == "A\n\nbbb"


def test_render_with_checklist_ordered_by_position() -> None:
    t = _task(title="Shop")
    items = [
        _item("milk", position=20),
        _item("bread", position=10),
        _item("eggs", position=30, done=True),
    ]
    rendered = render_task_for_search(t, items)
    # Items sorted by position; done item is struck through.
    assert rendered == "Shop\n\n- bread\n- milk\n- ~~eggs~~"


def test_render_strips_outer_whitespace() -> None:
    t = _task(title="   ")
    assert render_task_for_search(t, []) == ""


def test_content_hash_stable_and_changes_with_text() -> None:
    a = content_hash("hello")
    b = content_hash("hello")
    c = content_hash("hello world")
    assert a == b
    assert a != c
    # Hex sha256: 64 chars.
    assert len(a) == 64


# --- how the branches are merged -------------------------------------------
#
# The unified merge fuses branches by rank, and the reason is arithmetic
# rather than taste. A hit accumulates one RRF term per stage that ranked it,
# and `lexical_exact` carries weight 1.0 against 0.2 for stem and semantic.
# The scores below are the real ones observed on 2026-09-13 for the query
# "dove abbiamo deciso la regola di promozione del round embedder":
#
#   note, exact rank 1 + stem rank 1 + semantic rank 6
#       = 1.0/61 + 0.2/61 + 0.2/66 = 0.022702
#   task, stem rank 1 + semantic rank 1
#       = 0.2/61 + 0.2/61          = 0.006557
#
# Under the cross-branch relative floor that preceded this (keep hits within
# 0.4 of the top score) the cut sat at 0.009081, so the task that was rank 1
# in its own branch was dropped. On the frozen gold set that behaviour
# returned zero task hits on all twenty questions.

_NOTE_SCORE = 1.0 / 61 + 0.2 / 61 + 0.2 / 66
_TASK_SCORE = 0.2 / 61 + 0.2 / 61


def _hit(kind: str, score: float, tag: str) -> UnifiedHit:
    return UnifiedHit(
        kind=kind,
        blob_id=uuid.uuid5(uuid.NAMESPACE_OID, tag),
        task_id=None,
        title=tag,
        snippet=None,
        score=score,
        scope="org",
        model_id="m",
        scores_by_stage={},
        note_id=None,
        part_id=None,
    )


def test_a_lexical_note_no_longer_deletes_the_task_branch() -> None:
    """The measured defect, with the measured numbers. The note leads, which
    is right -- it matched a word exactly. The task survives, which is the
    fix: it was the best answer its own corpus had."""
    merged = _fuse_branches(
        [
            _hit("task", _TASK_SCORE, "task-1"),
            _hit("task", _TASK_SCORE * 0.9, "task-2"),
            _hit("note", _NOTE_SCORE, "note-1"),
        ]
    )
    assert [h.title for h in merged] == ["note-1", "task-1", "task-2"]


def test_within_one_rank_tier_the_stronger_hit_leads() -> None:
    """Rank makes the two comparable; score still orders them once they
    are. Otherwise the fix would trade erasure for arbitrary interleaving."""
    merged = _fuse_branches([_hit("task", 0.001, "weak"), _hit("note", 0.02, "strong")])
    assert [h.title for h in merged] == ["strong", "weak"]


def test_a_branch_cannot_take_two_places_before_the_other_has_one() -> None:
    """The property that makes silencing impossible: rank 2 of any branch
    sorts below rank 1 of every branch, whatever the scores say."""
    merged = _fuse_branches(
        [
            _hit("note", 0.9, "note-1"),
            _hit("note", 0.8, "note-2"),
            _hit("task", 0.001, "task-1"),
        ]
    )
    assert [h.title for h in merged] == ["note-1", "task-1", "note-2"]


def test_one_branch_alone_keeps_its_own_order() -> None:
    """A single-corpus search must be untouched by the fusion."""
    merged = _fuse_branches(
        [_hit("task", 0.3, "a"), _hit("task", 0.2, "b"), _hit("task", 0.1, "c")]
    )
    assert [h.title for h in merged] == ["a", "b", "c"]


def test_the_merge_is_a_permutation() -> None:
    """It reorders; it never drops. Dropping is what the old floor did."""
    hits = [_hit("note", 0.9, "n1"), _hit("task", 0.001, "t1"), _hit("blob", 0.5, "b1")]
    assert sorted(h.title or "" for h in _fuse_branches(hits)) == ["b1", "n1", "t1"]
