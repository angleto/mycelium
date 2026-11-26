"""``mycelium completion <shell>``: print the shell completion script.

The script only wires the shell to the CLI: at each TAB the shell runs
``mycelium`` with ``_MYCELIUM_COMPLETE`` set and Typer answers with the
candidates. So the file never goes stale when the CLI is upgraded, and a
package manager can generate it at install time (the Homebrew formula does).

Why this command and not Typer's ``--show-completion`` and
``--install-completion``, which stay because the same Typer switch registers
the completion classes that answer at each TAB (see ``main.py``): Typer 0.29.3, the latest
release on 2026-11-26, emits a zsh script that defines ``_mycelium_completion``
and calls ``compdef`` but never completes. Sourced from ``.zshrc`` that is
enough. Installed as ``_mycelium`` in ``$fpath``, which is where
``--install-completion`` itself puts it and where packagers put completion
files, zsh autoloads the file on the first TAB, the body only registers the
function, and that TAB completes nothing: measured in a pty, ``mycelium wor``
+ TAB rang the bell on the first press and became ``mycelium work`` on the
second. Upstream: fastapi/typer#1864 (same root cause), closed unmerged with
its PR #1866 because the project takes reports as discussions first.

Typer reads its zsh template from a private module dictionary, so there is no
seam to replace it through; overwriting that dictionary at import time would
be reaching into its internals. This command owns the zsh script and asks
Typer for the others. Those were not measured the way zsh was; the reasoning
for leaving them is that bash and fish load a completion file by sourcing it,
which runs the ``complete`` registration, rather than by autoloading it.

Remove the zsh template, and point the README back at Typer's options, once a
Typer release emits a script that completes when autoloaded. The protocol the
template speaks to (``_TYPER_COMPLETE_ARGS`` and ``complete_zsh``) is Typer's
too; ``tests/test_shell_completion.py`` drives it end to end so a change in it
fails there and not in somebody's shell.
"""

from __future__ import annotations

import enum
from typing import Annotated

import typer

# typer.completion is a public module and imports this name, but does not
# re-export it explicitly, so the checkers call it private. It is the one
# function that builds Typer's bash/fish/pwsh scripts; the alternative is
# copying them. test_shell_completion.py fails if it moves or changes shape.
from typer.completion import get_completion_script  # type: ignore[attr-defined]

# The console script name declared in pyproject.toml. Not taken from the
# Click context: under ``python -m mycelium_cli`` that is the interpreter's
# command line, and the script would register completion for the wrong word.
PROG_NAME = "mycelium"
COMPLETE_VAR = "_MYCELIUM_COMPLETE"


class Shell(enum.StrEnum):
    bash = "bash"
    zsh = "zsh"
    fish = "fish"
    pwsh = "pwsh"


# Named ``_mycelium`` because an autoloaded completion file must define the
# function its file is named after. ``loadautofunc`` is the zsh eval context
# of a function being autoloaded: there the body runs as the completion
# itself; sourced from a startup file it registers the function instead.
_ZSH_SCRIPT = f"""\
#compdef {PROG_NAME}

_{PROG_NAME}() {{
  eval $(env _TYPER_COMPLETE_ARGS="${{words[1,$CURRENT]}}" {COMPLETE_VAR}=complete_zsh {PROG_NAME})
}}

if [[ $zsh_eval_context[-1] == loadautofunc ]]; then
  _{PROG_NAME} "$@"
else
  compdef _{PROG_NAME} {PROG_NAME}
fi
"""


def completion_script(shell: Shell) -> str:
    if shell is Shell.zsh:
        return _ZSH_SCRIPT
    script = get_completion_script(prog_name=PROG_NAME, complete_var=COMPLETE_VAR, shell=shell)
    return script + "\n"


def completion(
    shell: Annotated[Shell, typer.Argument(help="Shell to print the completion script for.")],
) -> None:
    """Print the completion script for SHELL. Where to put it: see the README."""
    typer.echo(completion_script(shell), nl=False)
