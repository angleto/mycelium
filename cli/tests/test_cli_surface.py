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
    (["task", "add", "--help"], "--index-scope"),
    (["task", "edit", "--help"], "--index-scope"),
    (["task", "tag", "--help"], "add"),
    (["task", "comment", "--help"], "add"),
    (["task", "remind", "--help"], "add"),
    (["task", "attach", "--help"], "add"),
    (["task", "desc", "--help"], "append"),
    (["task", "desc", "prepend", "--help"], "--separator"),
    (["note", "--help"], "edit"),
    (["note", "edit", "--help"], "--task"),
    (["note", "add", "--help"], "--index-scope"),
    (["note", "edit", "--help"], "--index-scope"),
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
    (["worker", "--help"], "open"),
    (["worker", "close", "--help"], "MYCELIUM_WORKER"),
    (["task", "done", "--help"], "--worker"),
    (["task", "to", "--help"], "--worker"),
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


@pytest.mark.parametrize("verb", ["accept", "reject"])
def test_a_suggestion_is_decided_from_the_web_app(verb: str) -> None:
    # Deciding a suggestion is a person's act and the CLI's token is an
    # agent's: a command the server can only refuse is not offered.
    env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb"}
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "mycelium_cli", "annotate", verb, "x"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode != 0
    assert "No such command" in result.stdout + result.stderr


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb"}
    return subprocess.run(  # noqa: S603 - static argv, current interpreter, no shell
        [sys.executable, "-m", "mycelium_cli", *argv],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@pytest.mark.parametrize(
    "path", [[], ["task"], ["task", "edit"], ["auth", "mfa", "setup"], ["today"]]
)
def test_help_command_prints_what_dash_h_prints(path: list[str]) -> None:
    # `mycelium help X` is the spelling people type first; it must not be a
    # second rendering of the help that can drift from `X -h`.
    via_command = _run(["help", *path])
    via_flag = _run([*path, "-h"])
    assert via_command.returncode == 0, via_command.stderr
    assert via_command.stdout == via_flag.stdout


@pytest.mark.parametrize("path", [["nope"], ["task", "nope"], ["today", "extra"]])
def test_help_command_on_an_unknown_path_is_an_error_not_a_traceback(path: list[str]) -> None:
    # Seen failing on all three paths against the __main__ that called app()
    # directly: `python -m mycelium_cli` printed the CLIError as a traceback.
    result = _run(["help", *path])
    assert result.returncode == 1
    assert "no such command" in result.stderr
    assert "Traceback" not in result.stdout + result.stderr
