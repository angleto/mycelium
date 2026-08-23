"""Surface smoke tests: every registered command and sub-app responds to
``--help`` without crashing. Run offline; no backend required.

This catches the dumbest regressions (typo in a Typer signature, broken
import) at zero cost.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

# (argv, expected substring in help output)
_CASES = [
    (["task", "--help"], "edit"),
    (["task", "list", "--help"], "--sort"),
    (["task", "edit", "--help"], "--importance"),
    (["task", "tag", "--help"], "add"),
    (["task", "comment", "--help"], "add"),
    (["task", "remind", "--help"], "add"),
    (["task", "attach", "--help"], "add"),
    (["task", "desc", "--help"], "append"),
    (["task", "desc", "prepend", "--help"], "--separator"),
    (["note", "--help"], "edit"),
    (["note", "edit", "--help"], "--task"),
    (["note", "tag", "--help"], "add"),
    (["note", "attach", "--help"], "add"),
    (["note", "parts", "--help"], "prepend"),
    (["note", "parts", "replace", "--help"], "--count"),
    (["note", "parts", "set-body", "--help"], "--expected-version"),
    (["note", "link", "--help"], "supersedes"),
    (["note", "unlink", "--help"], "contradicts"),
    (["timer", "--help"], "entry"),
    (["timer", "entry", "--help"], "add"),
    (["auth", "--help"], "mfa"),
    (["auth", "mfa", "--help"], "setup"),
    (["auth", "mfa", "setup", "--help"], "--no-qr"),
    (["search", "--help"], "query"),
    (["what-now", "--help"], "--duration"),
    (["what-now", "--help"], "--min-priority"),
    (["what-now", "--help"], "--narrate"),
    (["today", "--help"], "--date"),
    (["week", "--help"], "--from"),
    (["open", "--help"], "ref"),
    (["client", "list", "--help"], "Personal"),
    (["project", "list", "--help"], "--client"),
    (["workspace", "list", "--help"], ""),
    (["notif", "list", "--help"], ""),
    (["schedule", "list", "--help"], ""),
    (["timer", "report", "--help"], "--group-by"),
    (["task", "graph", "--help"], "predecessors"),
    (["workflow", "--help"], "export"),
    (["workflow", "export", "--help"], "--file"),
    (["workflow", "import", "--help"], "--into"),
]


@pytest.mark.parametrize("argv,needle", _CASES)
def test_help_runs(argv: list[str], needle: str) -> None:
    # S603: argv is a static list of literals defined above; sys.executable
    # is trusted (the current interpreter). No shell, no user input.
    # NO_COLOR + TERM=dumb keep Typer/Click/Rich from inserting ANSI
    # SGR escapes between characters of an option name (e.g.
    # ``\x1b[36m-\x1b[0m\x1b[36m-sort`` for ``--sort``). With the
    # escapes interleaved the ``needle in stdout`` substring check
    # would miss every option name and the test would be a no-op in
    # any environment where Rich detects color (CI does, since uv /
    # GitHub Actions set FORCE_COLOR=1 by default).
    env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb"}
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "mycelium_cli", *argv],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"`flow {' '.join(argv)}` exited {result.returncode}:\n{result.stderr}"
    )
    if needle:
        assert needle in result.stdout, f"`flow {' '.join(argv)}` help did not mention '{needle}'."
