"""``flow notif`` — list and dismiss notifications."""

from __future__ import annotations

import typer

from flow_cli.cmds._common import client, get_json, short_id
from flow_cli.ui import emit_json, emit_table, info, json_mode, success

app = typer.Typer(no_args_is_help=True, help="Notifications: list, dismiss.")


@app.command("list")
def list_(
    limit: int = typer.Option(30, "--limit", "-n", min=1, max=200),
) -> None:
    """List recent notifications."""
    with client() as c:
        rows = get_json(c.get("/notifications"))
    rows = rows[:limit]
    if json_mode():
        emit_json(rows)
        return
    if not rows:
        info("[dim]no notifications.[/dim]")
        return
    emit_table(
        None,
        ["id", "kind", "channel", "title", "status", "created"],
        [
            (
                short_id(r.get("id")),
                r.get("kind"),
                r.get("channel"),
                r.get("title"),
                r.get("status"),
                str(r.get("created_at") or "")[:16],
            )
            for r in rows
        ],
    )


@app.command()
def dismiss(notification_id: str = typer.Argument(...)) -> None:
    """Mark a notification as dismissed (deletes the dispatch row)."""
    with client() as c:
        resp = c.delete(f"/notifications/{notification_id}")
        if resp.status_code not in (200, 204):
            get_json(resp)
    success(f"dismissed {short_id(notification_id)}")
