"""The description-edit fold, which keeps who did what apart."""

from __future__ import annotations

import datetime as dt

from mycelium_core.services.work_digest import DigestEvent, EventKind, _fold_description_edits

T0 = dt.datetime(2026, 11, 22, 9, tzinfo=dt.UTC)


def _edit(minute: int, actor: str, *, via_agent: bool = False) -> DigestEvent:
    return DigestEvent(
        at=T0 + dt.timedelta(minutes=minute),
        kind=EventKind.description_edited,
        actor=actor,
        via_agent=via_agent,
    )


def test_consecutive_edits_by_one_author_fold_and_two_authors_stay_two() -> None:
    out = _fold_description_edits(
        [_edit(0, "ann"), _edit(1, "ann"), _edit(2, "bob"), _edit(3, "bob", via_agent=True)]
    )
    assert [(e.actor, e.via_agent, e.count) for e in out] == [
        ("ann", False, 2),
        ("bob", False, 1),
        ("bob", True, 1),
    ]
    assert out[0].at == T0 + dt.timedelta(minutes=1)
