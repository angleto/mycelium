"""``mycelium worker`` -- open, list and close the working session the CLI
moves tasks as.

The CLI authenticates with the agent token ``mycelium auth login`` stored,
and that file is readable by every process of the user, so the server
treats a CLI call as an agent's (docs/adr/0072). An agent moves a task only
while holding it, and it holds it as a worker: an id the server mints, one
per session, because every session on the machine shares the credential
and nothing else tells them apart (docs/adr/0063).

So the worker id lives in the shell, not in the profile. A profile is
shared by every terminal and every agent that runs the CLI as this user; a
worker stored there would make all of them one worker, which is the
collapse the worker exists to prevent. ``eval "$(mycelium worker open)"``
sets it for the shell it runs in and for nothing else.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import uuid
from collections.abc import Iterator
from typing import Annotated

import typer

from mycelium_cli.cmds._common import client, get_json
from mycelium_cli.http import CLIError
from mycelium_cli.ui import emit_json, emit_table, err, json_mode

ENV_VAR = "MYCELIUM_WORKER"

app = typer.Typer(
    no_args_is_help=True,
    help=f"The working session task moves are made as (exported as {ENV_VAR}).",
)


def current_worker(explicit: str | None = None) -> str | None:
    """The worker this invocation acts as: the flag, else the environment."""
    value = (explicit or os.environ.get(ENV_VAR) or "").strip()
    return value or None


def parse_worker(raw: str) -> str:
    """The worker id in its canonical form, or a CLI error naming the value."""
    try:
        return str(uuid.UUID(raw))
    except ValueError as exc:
        raise CLIError(
            f"'{raw}' is not a worker id.",
            hint='Run: eval "$(mycelium worker open)"',
        ) from exc


def same_worker(a: object, b: str) -> bool:
    try:
        return uuid.UUID(str(a)) == uuid.UUID(b)
    except ValueError:
        return False


@contextlib.contextmanager
def explain_worker_errors(worker_id: str) -> Iterator[None]:
    """Turn the server's "no such worker" into the CLI's repair.

    A worker closes when it is closed and when it sits idle past the
    server's limit, and the variable outlives it. The server's message
    names its MCP tools, which a shell cannot run."""
    try:
        yield
    except CLIError as exc:
        if "worker.not_found" in str(exc):
            raise CLIError(
                f"worker {worker_id[:8]} is closed or unknown.",
                hint='Run: eval "$(mycelium worker open)"',
            ) from exc
        raise


@app.command("open")
def open_(
    label: Annotated[
        str | None,
        typer.Option("--label", "-l", help="Shown beside the tasks this session holds."),
    ] = None,
) -> None:
    """Open a working session and print the line that exports its id.

    Run it as ``eval "$(mycelium worker open)"``: the export is the only
    thing written to stdout, so the shell picks up the id and nothing else.
    """
    with client() as c:
        worker = get_json(c.post("/tasks/workers", json={"label": label} if label else {}))
    if json_mode():
        emit_json(worker)
        return
    typer.echo(f"export {ENV_VAR}={shlex.quote(str(worker['id']))}")
    # Stderr, so ``eval`` reads the export and nothing else.
    err().print(f"[green]worker {str(worker['id'])[:8]} open[/green]")


@app.command("close")
def close(
    worker_id: Annotated[
        str | None,
        typer.Argument(help=f"The worker to close. Default: ${ENV_VAR}."),
    ] = None,
) -> None:
    """Close a working session, giving back every task it holds.

    ``eval "$(mycelium worker close)"`` closes this shell's worker and
    unsets the variable."""
    target = current_worker(worker_id)
    if target is None:
        raise CLIError(f"no worker to close: pass its id, or set {ENV_VAR}.")
    with client() as c:
        closed = get_json(c.post(f"/tasks/workers/{target}/close"))
    if json_mode():
        emit_json(closed)
        return
    freed = closed.get("released_tasks") or []
    err().print(f"[green]worker {target[:8]} closed; {len(freed)} task(s) given back[/green]")
    if worker_id is None:
        # The variable still names a closed worker; say how to drop it
        # rather than leave the next move to fail on it.
        typer.echo(f"unset {ENV_VAR}")


@app.command("list")
def list_(
    include_closed: Annotated[
        bool, typer.Option("--all", "-a", help="Include closed sessions.")
    ] = False,
) -> None:
    """The working sessions you have open."""
    with client() as c:
        rows = get_json(
            c.get(
                "/tasks/workers",
                params={"mine_only": "true", "include_closed": str(include_closed).lower()},
            )
        )
    mine = current_worker()
    emit_table(
        None,
        ["id", "label", "opened", "closed", "this shell"],
        [
            (
                str(r.get("id", "")),
                r.get("label") or "",
                r.get("opened_at") or "",
                r.get("closed_at") or "",
                "yes" if mine and str(r.get("id")) == mine else "",
            )
            for r in rows
        ],
    )
