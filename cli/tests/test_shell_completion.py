"""`mycelium completion`: the scripts it prints, and the zsh one autoloaded.

The zsh test needs zsh on PATH and fails without it rather than skipping:
CI installs it, and a skipped test here would leave the one defect this
command exists for unguarded.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _shim(tmp_path: Path) -> Path:
    # The scripts call `mycelium` by name, and Typer derives the variable it
    # answers on from the program name, so the CLI must run under that name:
    # `python -m mycelium_cli` would answer on a different variable.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "mycelium"
    shim.write_text(
        f"#!{sys.executable}\nimport sys\nfrom mycelium_cli.main import main\nsys.exit(main())\n"
    )
    shim.chmod(0o755)
    return bindir


def _env(bindir: Path, **extra: str) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "NO_COLOR": "1",
        "TERM": "dumb",
        **extra,
    }


def _run(bindir: Path, argv: list[str], **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - static argv, our own shim, no shell
        [str(bindir / "mycelium"), *argv],
        capture_output=True,
        text=True,
        env=_env(bindir, **extra),
        check=False,
    )


def test_the_protocol_the_zsh_script_speaks_is_answered(tmp_path: Path) -> None:
    # The zsh template is ours but the protocol it speaks is Typer's: this is
    # what fails if a Typer release renames the variables or the instruction.
    bindir = _shim(tmp_path)
    result = _run(
        bindir,
        [],
        _MYCELIUM_COMPLETE="complete_zsh",
        _TYPER_COMPLETE_ARGS="mycelium wor",
    )
    assert result.returncode == 0, result.stderr
    assert '"worker"' in result.stdout
    assert '"workspace"' in result.stdout


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish", "pwsh"])
def test_every_shell_gets_a_script_wired_to_the_cli(tmp_path: Path, shell: str) -> None:
    result = _run(_shim(tmp_path), ["completion", shell])
    assert result.returncode == 0, result.stderr
    assert "_MYCELIUM_COMPLETE" in result.stdout


def test_an_unknown_shell_is_refused(tmp_path: Path) -> None:
    result = _run(_shim(tmp_path), ["completion", "tcsh"])
    assert result.returncode != 0


def test_the_zsh_script_completes_on_the_tab_that_autoloads_it(tmp_path: Path) -> None:
    # The defect: installed as _mycelium in $fpath, Typer's script only
    # registered a function on the first TAB and completed nothing. Seen
    # failing against Typer 0.29.3's script (`mycelium --show-completion zsh`
    # written to the same file): COMPDEF printed, no candidates.
    zsh = shutil.which("zsh")
    assert zsh, "zsh is required for this test (CI installs it)"
    bindir = _shim(tmp_path)
    fdir = tmp_path / "functions"
    fdir.mkdir()
    (fdir / "_mycelium").write_text(_run(bindir, ["completion", "zsh"]).stdout)
    # compsys is stubbed: _arguments is what receives the candidates, so it
    # printing them is the completion having happened on this first call.
    program = (
        f"fpath=({fdir} $fpath)\n"
        "compdef() { print -r -- COMPDEF }\n"
        "_arguments() { print -r -- CANDIDATES $@ }\n"
        "words=(mycelium wor); CURRENT=2\n"
        "autoload -Uz _mycelium\n"
        "_mycelium\n"
    )
    result = subprocess.run(  # noqa: S603 - zsh from PATH, program written above
        [zsh, "-f", "-c", program],
        capture_output=True,
        text=True,
        env=_env(bindir),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "CANDIDATES" in result.stdout
    assert "worker" in result.stdout
