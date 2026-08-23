"""``mycelium workflow`` — list, export and import workflow definitions.

Export writes the interchange document of docs/adr/0052 to a file (or
stdout); import sends one back. The rules of that format live in the
server, not here: this module reads and writes bytes, and lets the API
say whether a document is acceptable. That is the whole point of the
endpoints existing, and it is why the CLI and the SPA can never disagree
about a file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from mycelium_cli.cmds._common import client, get_json, resolve_id
from mycelium_cli.http import CLIError, raise_for_response
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


@app.command("import")
def import_(
    file: Annotated[
        str,
        typer.Option("--file", "-f", help=f"Document to import ('{STDIO}' for stdin)."),
    ],
    into: Annotated[
        str | None,
        typer.Option(
            "--into",
            help="Replace this workflow (id or unique prefix). Omit to create a new one.",
        ),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", help="Override the name in the file. New workflows only."),
    ] = None,
) -> None:
    """Import a workflow document.

    Without ``--into`` this creates a new workflow. With ``--into`` it
    REPLACES that workflow's configuration: states are matched by name,
    so the ones the document names again keep their identity and their
    tasks, and one it drops is deleted -- which the server refuses while
    any task still sits in it.
    """
    raw = sys.stdin.read() if file == STDIO else Path(file).read_text(encoding="utf-8")
    try:
        document: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CLIError(f"{file}: not valid JSON ({exc.msg} at line {exc.lineno})") from exc
    if not isinstance(document, dict):
        raise CLIError(f"{file}: expected a JSON object, got {type(document).__name__}")
    if into is not None and name is not None:
        # Silently ignoring it would look like a rename that did not
        # happen: the name of an existing workflow comes from the file.
        raise CLIError("--name applies to a new workflow; drop --into, or edit the file.")

    with client(role="owner") as c:
        if into is not None:
            workflow_id = resolve_id(c, into, endpoint="/workflows", kind="workflow")
            resp = c.post(f"/workflows/{workflow_id}/import", json=document)
            raise_for_response(resp)
            success(f"replaced workflow {workflow_id[:8]} from {file}")
            return
        created = get_json(
            c.post("/workflows/import", json=document, params={"name": name} if name else None)
        )
    if json_mode():
        emit_json(created)
        return
    success(f"created workflow {str(created.get('id', ''))[:8]} ({created.get('name')})")
