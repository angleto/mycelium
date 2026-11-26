"""``mycelium help [COMMAND...]``: the help of any command, reached by name.

``-h`` / ``--help`` is still the primary form; this is the spelling people
type first (``git help``, ``cargo help``) and it must print exactly what
``--help`` prints for the same path. So it does not render help itself: it
walks the Click tree from the root by the names given and asks the command
it lands on for its own help, which is the only place that text lives.
"""

from __future__ import annotations

from typing import Annotated

import typer
from typer.core import TyperGroup

from mycelium_cli.http import CLIError


def help_command(
    ctx: typer.Context,
    command: Annotated[
        list[str] | None,
        typer.Argument(help="Command path, e.g. 'task edit'. Omit for the top-level help."),
    ] = None,
) -> None:
    """Show the help of a command: `mycelium help task edit` is `mycelium task edit -h`."""
    target = ctx.find_root()
    for name in command or []:
        group = target.command
        sub = group.get_command(target, name) if isinstance(group, TyperGroup) else None
        if sub is None:
            path = " ".join([target.command_path, name])
            raise CLIError(
                f"no such command: {path}",
                hint=f"Run: {target.command_path} -h",
            )
        # resilient_parsing: build the context without validating arguments
        # or honouring no_args_is_help, both of which would exit before the
        # help could be read.
        target = sub.make_context(name, [], parent=target, resilient_parsing=True)
    # The same call --help makes. Under rich markup get_help() prints to the
    # console itself and returns "", and the echo adds the trailing newline
    # --help also adds: echoing only a non-empty result left the two outputs
    # one blank line apart, which is how this line was arrived at.
    typer.echo(target.get_help(), color=target.color)
