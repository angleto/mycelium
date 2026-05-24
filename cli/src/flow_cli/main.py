"""Top-level Typer app for ``flow``.

Sub-commands live in :mod:`flow_cli.cmds.*`. Each sub-module exposes a
``app`` (or a single command callable). This module assembles them and
installs a uniform error-rendering hook so ``CLIError`` never surfaces a
Python traceback to a user who just made a typo.
"""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from flow_cli import __version__
from flow_cli.cmds import auth as auth_cmd
from flow_cli.cmds import notes as notes_cmd
from flow_cli.cmds import tags as tags_cmd
from flow_cli.cmds import tasks as tasks_cmd
from flow_cli.cmds import timer as timer_cmd
from flow_cli.cmds import today as today_cmd
from flow_cli.http import CLIError
from flow_cli.ui import fail, set_json_mode

app = typer.Typer(
    name="flow",
    add_completion=True,
    no_args_is_help=True,
    help="Keyboard-first CLI for Flow (tasks, notes, time, calendar).",
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"flow-cli {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    json: Annotated[
        bool,
        typer.Option(
            "--json", help="Emit machine-readable JSON (suppresses tables/colour).", is_flag=True
        ),
    ] = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show version and exit.",
            is_eager=True,
            callback=_version_callback,
        ),
    ] = False,
) -> None:
    set_json_mode(json)


app.add_typer(auth_cmd.app, name="auth")
app.add_typer(tasks_cmd.app, name="task")
app.add_typer(notes_cmd.app, name="note")
app.add_typer(timer_cmd.app, name="timer")
app.add_typer(tags_cmd.app, name="tag")
app.command(name="today", help="Show today's running timer and due tasks.")(today_cmd.today)


def _entrypoint() -> int:
    try:
        app()
    except CLIError as exc:
        fail(str(exc), hint=exc.hint)
        return 1
    except typer.Exit as exc:
        return exc.exit_code
    return 0


def main() -> int:
    return _entrypoint()


if __name__ == "__main__":
    sys.exit(main())
