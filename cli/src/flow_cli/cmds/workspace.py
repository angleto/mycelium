"""``flow workspace`` — list workspaces the user belongs to.

Switching a profile to another workspace requires re-issuing a PAT
(agent tokens are bound to one workspace), which is identical to
``flow auth login --workspace <id> --profile <name>``. We surface that
in the ``switch`` command output instead of fudging it locally.
"""

from __future__ import annotations

import typer

from flow_cli.cmds._common import active_profile, client, get_json, short_id
from flow_cli.ui import emit_json, emit_table, info, json_mode, out

app = typer.Typer(no_args_is_help=True, help="Workspaces visible to your account.")


@app.command("list")
def list_() -> None:
    """List workspaces the authenticated user is a member of."""
    with client() as c:
        rows = get_json(c.get("/workspaces"))
    if json_mode():
        emit_json(rows)
        return
    name, _, cred = active_profile()
    bound = cred.workspace_id or ""
    out_rows = []
    for r in rows:
        wid = str(r.get("id", ""))
        marker = "← active" if wid == bound else ""
        out_rows.append((short_id(wid), r.get("name"), r.get("role"), r.get("status"), marker))
    emit_table(None, ["id", "name", "role", "status", ""], out_rows)
    info(f"\n[dim]active profile '{name}' bound to workspace {short_id(bound)}.[/dim]")


@app.command()
def switch(
    workspace: str = typer.Argument(..., help="Workspace name or UUID."),
    profile: str = typer.Option(
        "default", "--profile", help="Local profile to bind to the new workspace."
    ),
) -> None:
    """Print the command to re-mint a PAT for another workspace.

    Agent tokens are workspace-scoped (RLS-enforced); there is no
    side-effect switch. We tell you the exact ``flow auth login`` to run.
    """
    _, prof, _ = active_profile()
    out().print(
        f"To bind profile [bold]{profile}[/bold] to workspace [bold]{workspace}[/bold]:\n"
        f"  flow auth login \\\n"
        f"    --base-url {prof.base_url} \\\n"
        f"    --workspace {workspace} \\\n"
        f"    --profile {profile}"
    )
