"""``mycelium workflow`` -- list and export workflow definitions.

Export writes the interchange document of docs/adr/0052 to a file (or
stdout), and the rules of that format live in the server: this module
writes the server's bytes and nothing else.

There is no import. A workflow says which state closes work and so where
an agent's work needs another worker's check, which makes writing one a
person's act (docs/adr/0072). The CLI authenticates with a token every
process of the user can read, so the server treats it as an agent and
refuses the write. A document is imported from the web app.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from mycelium_cli.cmds._common import client, get_json, resolve_id
from mycelium_cli.http import raise_for_response
from mycelium_cli.ui import emit_json, emit_table, json_mode, success

app = typer.Typer(no_args_is_help=True, help="Workflow definitions (states and transitions).")

# Path convention shared with the rest of the CLI's file arguments: a
# bare dash means the stream, not a file called "-".
STDIO = "-"


def _slug(name: str) -> str:
    keep = [ch if ch.isalnum() or ch in "-_" else "-" for ch in name.strip()]
    return "".join(keep).strip("-")[:120] or "workflow"


@app.command("list")
def list_(
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Include states and transitions.")
    ] = False,
) -> None:
    """List the workspace's workflows."""
    with client() as c:
        rows = get_json(c.get("/workflows"))
        if verbose:
            for row in rows:
                states = get_json(c.get(f"/workflows/{row['id']}/states"))
                row["states"] = [s["name"] for s in states]
    if json_mode() or verbose:
        emit_json(rows)
        return
    emit_table(
        None,
        ["id", "name", "default", "description"],
        [
            (
                str(r.get("id", ""))[:8],
                r.get("name"),
                "yes" if r.get("is_default") else "",
                (r.get("description") or "")[:60],
            )
            for r in rows
        ],
    )


@app.command("export")
def export(
    workflow: Annotated[str, typer.Argument(help="Workflow id, or a unique id prefix.")],
    file: Annotated[
        str | None,
        typer.Option(
            "--file",
            "-f",
            help=f"Write here ('{STDIO}' for stdout). Default: workflow-<name>.json in the cwd.",
        ),
    ] = None,
) -> None:
    """Write a workflow out as a portable JSON document.

    The document carries no database identity, so the file can be
    imported into any workspace.
    """
    with client() as c:
        workflow_id = resolve_id(c, workflow, endpoint="/workflows", kind="workflow")
        resp = c.get(f"/workflows/{workflow_id}/export")
        raise_for_response(resp)
        # The server's bytes, verbatim: re-serialising here would make
        # two exports of the same workflow differ by whitespace.
        payload = resp.text
    if file == STDIO:
        sys.stdout.write(payload)
        return
    if file is None:
        name = json.loads(payload).get("name", "workflow")
        file = f"workflow-{_slug(str(name))}.json"
    Path(file).write_text(payload, encoding="utf-8")
    success(f"exported to {file}")
