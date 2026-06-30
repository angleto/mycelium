"""Rendering helpers (Rich + JSON) shared by all commands.

The ``--json`` global flag flips every formatter into machine-readable
mode, so a script can do ``mycelium task list --json | jq '.[].id'`` without
the CLI ever fighting it.
"""

from __future__ import annotations

import datetime as dt
import json as _json
import os
import sys
import tempfile
from collections.abc import Iterable, Sequence
from subprocess import run
from typing import Any

from rich.console import Console
from rich.table import Table

_console: Console | None = None
_err_console: Console | None = None
_json_mode = False


def set_json_mode(on: bool) -> None:
    global _json_mode
    _json_mode = on


def json_mode() -> bool:
    return _json_mode


def out() -> Console:
    global _console
    if _console is None:
        _console = Console(highlight=False, soft_wrap=False)
    return _console


def err() -> Console:
    global _err_console
    if _err_console is None:
        _err_console = Console(stderr=True, highlight=False, soft_wrap=False)
    return _err_console


def emit_json(payload: Any) -> None:
    _console_or_plain_print(_json.dumps(payload, indent=2, default=_json_default))


def emit_table(
    title: str | None,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
) -> None:
    if _json_mode:
        emit_json([dict(zip(columns, r, strict=False)) for r in rows])
        return
    table = Table(title=title, show_lines=False)
    for col in columns:
        table.add_column(col, overflow="fold")
    for r in rows:
        table.add_row(*[_fmt_cell(v) for v in r])
    out().print(table)


def info(msg: str) -> None:
    if _json_mode:
        return
    out().print(msg)


def success(msg: str) -> None:
    if _json_mode:
        return
    out().print(f"[green]{msg}[/green]")


def warn(msg: str) -> None:
    if _json_mode:
        err().print(f"[yellow]{msg}[/yellow]")
    else:
        err().print(f"[yellow]warning:[/yellow] {msg}")


def fail(msg: str, *, hint: str | None = None) -> None:
    if _json_mode:
        err().print(_json.dumps({"error": msg, "hint": hint}))
    else:
        err().print(f"[red]error:[/red] {msg}")
        if hint:
            err().print(f"[dim]hint:[/dim] {hint}")


def edit_in_editor(initial: str = "", *, suffix: str = ".md") -> str:
    """Open ``$EDITOR`` on a tempfile preloaded with ``initial`` and
    return the saved buffer. An empty buffer signals "abort" upstream.
    """
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nvim"
    with tempfile.NamedTemporaryFile("w+", suffix=suffix, delete=False) as fh:
        path = fh.name
        fh.write(initial)
    try:
        proc = run([editor, path], check=False)  # noqa: S603
        if proc.returncode != 0:
            return ""
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _fmt_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, dt.datetime):
        return v.astimezone().strftime("%Y-%m-%d %H:%M")
    if isinstance(v, dt.date):
        return v.isoformat()
    return str(v)


def _json_default(v: Any) -> Any:
    if isinstance(v, dt.datetime | dt.date):
        return v.isoformat()
    raise TypeError(f"not JSON-serialisable: {type(v).__name__}")


def _console_or_plain_print(s: str) -> None:
    # When stdout is piped to ``jq``/``less`` we want raw output, not the
    # Rich Console (which can add control chars even in dumb mode).
    if sys.stdout.isatty() and not _json_mode:
        out().print(s)
    else:
        sys.stdout.write(s + "\n")
