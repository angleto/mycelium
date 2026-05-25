"""Pure-function unit tests for the task-search rendering / hashing.

The DB-driven paths (listeners, /search endpoint, embedder fallback)
are exercised by ``api/tests/test_search_unified.py``; this file
covers the small synchronous helpers that don't need a session.
"""

from __future__ import annotations

import datetime as dt
import uuid

from flow_core.models.task import Necessity, Task
from flow_core.models.task_checklist_item import TaskChecklistItem
from flow_core.services.task_search import (
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
