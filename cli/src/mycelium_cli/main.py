"""Top-level Typer app for ``flow``.

Sub-commands live in :mod:`mycelium_cli.cmds.*`. Each sub-module exposes a
``app`` (or a single command callable). This module assembles them and
installs a uniform error-rendering hook so ``CLIError`` never surfaces a
Python traceback to a user who just made a typo.
"""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from mycelium_cli import __version__
from mycelium_cli.cmds import annotations as annotations_cmd
from mycelium_cli.cmds import attachments as attachments_cmd
from mycelium_cli.cmds import auth as auth_cmd
from mycelium_cli.cmds import clients as clients_cmd
from mycelium_cli.cmds import invoices as invoices_cmd
from mycelium_cli.cmds import notes as notes_cmd
from mycelium_cli.cmds import notifications as notif_cmd
from mycelium_cli.cmds import open_url as open_cmd
from mycelium_cli.cmds import schedule as schedule_cmd
from mycelium_cli.cmds import search as search_cmd
from mycelium_cli.cmds import tags as tags_cmd
from mycelium_cli.cmds import tasks as tasks_cmd
from mycelium_cli.cmds import timer as timer_cmd
from mycelium_cli.cmds import today as today_cmd
from mycelium_cli.cmds import what_now as what_now_cmd
from mycelium_cli.cmds import workspace as workspace_cmd
from mycelium_cli.http import CLIError
from mycelium_cli.ui import fail, set_json_mode

app = typer.Typer(
    name="mycelium",
    add_completion=True,
    no_args_is_help=True,
    help="Keyboard-first CLI for Mycelium (tasks, notes, time, calendar).",
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"mycelium-cli {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit machine-readable JSON (suppresses tables/colour).",
            is_flag=True,
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


# Sub-apps
app.add_typer(auth_cmd.app, name="auth")
app.add_typer(tasks_cmd.app, name="task")
app.add_typer(notes_cmd.app, name="note")
app.add_typer(annotations_cmd.app, name="annotate")
app.add_typer(timer_cmd.app, name="timer")
app.add_typer(tags_cmd.app, name="tag")
app.add_typer(clients_cmd.clients_app, name="client")
app.add_typer(clients_cmd.projects_app, name="project")
app.add_typer(invoices_cmd.app, name="invoice")
app.add_typer(workspace_cmd.app, name="workspace")
app.add_typer(notif_cmd.app, name="notif")
app.add_typer(schedule_cmd.app, name="schedule")
app.add_typer(attachments_cmd.app, name="attachments")

# Top-level single commands
app.command(name="today", help="Today's running timer + tasks (+ --date / --tz).")(today_cmd.today)
app.command(name="week", help="Next 7 days of scheduled work.")(today_cmd.week)
app.command(name="search", help="Hybrid (keyword + semantic) search.")(search_cmd.search)
app.command(name="what-now", help="Advisor: feasible tasks for a window/location.")(
    what_now_cmd.what_now
)
app.command(name="open", help="Open the SPA on a resource (browser fallback).")(open_cmd.open_url)


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
