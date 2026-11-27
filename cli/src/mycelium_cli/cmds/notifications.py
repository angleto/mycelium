"""``mycelium notif`` — list, dismiss and send notifications."""

from __future__ import annotations

import typer

from mycelium_cli.cmds._common import client, get_json, resolve_id, short_id
from mycelium_cli.ui import emit_json, emit_table, info, json_mode, success

app = typer.Typer(no_args_is_help=True, help="Notifications: list, dismiss, send to yourself.")


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
def dismiss(
    notification_id: str = typer.Argument(
        ..., help="Notification UUID (full or unique short prefix)."
    ),
) -> None:
    """Mark a notification as dismissed (deletes the dispatch row)."""
    with client() as c:
        full = resolve_id(c, notification_id, endpoint="/notifications", kind="notification")
        resp = c.delete(f"/notifications/{full}")
        if resp.status_code not in (200, 204):
            get_json(resp)
    success(f"dismissed {short_id(notification_id)}")


@app.command()
def send(
    title: str = typer.Argument(..., help="The message's first line (at most 120 characters)."),
    body: str = typer.Option("", "--body", "-b", help="More text below the title."),
    key: str | None = typer.Option(
        None,
        "--key",
        "-k",
        help="Send once per key: repeating it is a no-op on channels it already went to.",
    ),
) -> None:
    """Send a message to yourself, on your own channels.

    It goes to every channel of yours that can reach you in this workspace
    (e-mail, linked Telegram, subscribed browsers), within about a minute.
    There is no recipient to choose: it only ever reaches you."""
    payload: dict[str, str] = {"title": title, "body": body}
    if key is not None:
        payload["key"] = key
    with client() as c:
        out = get_json(c.post("/notifications/self", json=payload))
    if json_mode():
        emit_json(out)
        return
    if out.get("queued"):
        success(f"queued on {', '.join(out['queued'])}: goes out within about a minute")
    if out.get("already_queued"):
        info(f"already sent with this key on {', '.join(out['already_queued'])}")
