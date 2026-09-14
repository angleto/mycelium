"""A failed editor changes nothing.

``edit_in_editor`` returned ``""`` for two different events: the user
emptied the buffer and saved, and the editor FAILED (non-zero exit). Its
own docstring said an empty buffer "signals abort upstream", and no
caller had ever implemented that. The three commands that edit EXISTING
text -- a note body, a task description, a note part -- wrote the empty
string straight back.

On a single-part note that is data loss with a second helping: the flat
body writer blanks part 0 AND re-derives the title from nothing, so the
note loses its name too. ``notes.update_note`` refuses a flat write only
over a MULTI-part note, which is the one case that was protected.

The fix is at the origin rather than in three guards: ``None`` means "do
not write", ``""`` still means the user really did empty it, which stays
a legal edit through the explicit route.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from typing import Any

import pytest

from mycelium_cli import ui


class _Proc:
    """The shape ``subprocess.run`` returns, with the code we care about."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


@pytest.fixture()
def _editor(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """A fake $EDITOR whose exit code and written buffer the test sets."""
    state: dict[str, Any] = {"returncode": 0, "write": None}

    def fake_run(cmd: list[str], **_kw: Any) -> _Proc:
        if state["write"] is not None:
            with open(cmd[1], "w", encoding="utf-8") as fh:
                fh.write(state["write"])
        return _Proc(state["returncode"])

    monkeypatch.setattr(ui, "run", fake_run)
    monkeypatch.setenv("EDITOR", "fake-editor")
    yield state


def test_a_failed_editor_is_not_an_empty_body(_editor: dict[str, Any]) -> None:
    """The distinction the whole fix rests on. Same empty result on the
    wire before; two different answers now."""
    _editor["returncode"] = 1
    assert ui.edit_in_editor("un corpo che esiste\n") is None


def test_a_user_who_really_empties_the_buffer_still_gets_an_empty_body(
    _editor: dict[str, Any],
) -> None:
    """The half a blunt "empty means abort" rule would have broken.
    Emptying a note on purpose is a legal edit and must stay possible."""
    _editor["returncode"] = 0
    _editor["write"] = ""
    assert ui.edit_in_editor("un corpo che esiste\n") == ""


def test_an_ordinary_save_comes_back_verbatim(_editor: dict[str, Any]) -> None:
    """Including the trailing whitespace ``body_or_none`` exists to
    preserve: a markdown hard break is two spaces at end of line."""
    _editor["returncode"] = 0
    _editor["write"] = "riga  \n\n    codice indentato\n"
    assert ui.edit_in_editor("prima") == "riga  \n\n    codice indentato\n"


def test_an_editor_that_writes_nothing_leaves_the_initial_text(
    _editor: dict[str, Any],
) -> None:
    """Quitting without saving (``:q`` in vim) exits ZERO and leaves the
    tempfile as it was, so the buffer equals the original. That is not an
    abort and must not be reported as one: the callers' own "nothing
    changed" branch is what handles it."""
    _editor["returncode"] = 0
    _editor["write"] = None
    assert ui.edit_in_editor("testo originale\n") == "testo originale\n"


def test_the_helper_is_the_only_place_the_two_are_told_apart() -> None:
    """The contract, asserted so a future caller cannot re-introduce the
    conflation by coercing the None away at its own call site."""
    assert "str | None" in str(ui.edit_in_editor.__annotations__["return"]) or (
        ui.edit_in_editor.__annotations__["return"] in ("str | None", "Optional[str]")
    )


def test_subprocess_is_not_actually_invoked(_editor: dict[str, Any]) -> None:
    """A guard on the fixture itself: if the monkeypatch ever stopped
    biting, these tests would launch a real editor in CI and hang."""
    _editor["returncode"] = 0
    _editor["write"] = "x"
    assert ui.run is not subprocess.run
    assert ui.edit_in_editor("") == "x"
