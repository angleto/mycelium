"""Worker job-registration wiring (tasks 44b4c212 / d8664631).

The garden seasonal-rule loop mutates note maturity on real data, so it
is opt-in: it must appear in the gathered job set iff
``garden_loop_enabled`` is true, and every always-on loop must be present
regardless. This guards against the loop silently dropping out of the
worker entrypoint again (it was never wired before these tasks).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mycelium_worker import (
    dispatch,
    email_sync,
    embedding_migration,
    garden,
    main,
    reminders,
)


def _jobs_with(*, garden_loop_enabled: bool, monkeypatch: pytest.MonkeyPatch) -> list[object]:
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(garden_loop_enabled=garden_loop_enabled),
    )
    return list(main._enabled_jobs())


def test_garden_loop_excluded_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    assert garden.run_forever not in _jobs_with(garden_loop_enabled=False, monkeypatch=monkeypatch)


def test_garden_loop_included_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    assert garden.run_forever in _jobs_with(garden_loop_enabled=True, monkeypatch=monkeypatch)


def test_always_on_jobs_present_regardless_of_garden(monkeypatch: pytest.MonkeyPatch) -> None:
    always_on = (
        dispatch.run_forever,
        email_sync.run_forever,
        embedding_migration.run_forever,
        reminders.run_forever,
    )
    for enabled in (False, True):
        jobs = _jobs_with(garden_loop_enabled=enabled, monkeypatch=monkeypatch)
        for job in always_on:
            assert job in jobs
