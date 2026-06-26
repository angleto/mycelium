"""``mycelium attachments`` — mint a scoped, multi-use download grant."""

from __future__ import annotations

import shlex

import typer

from mycelium_cli.cmds._common import client, get_json, resolve_id, short_id
from mycelium_cli.http import CLIError
from mycelium_cli.ui import emit_json, json_mode, success, warn

app = typer.Typer(no_args_is_help=True, help="Attachments: scoped download grants.")


def _resolve_parent(c: object, kind: str, partial: str) -> str:
    endpoint = "/tasks" if kind == "task" else "/notes"
    return resolve_id(c, partial, endpoint=endpoint, kind=kind)  # type: ignore[arg-type]


@app.command("download-capability")
def download_capability(
    parent_kind: str = typer.Argument(..., help="'task' or 'note'."),
    parent_id: str = typer.Argument(
        ...,
        help="The task/note id (full UUID or unique short prefix).",
    ),
    ttl_seconds: int = typer.Option(
        300, "--ttl", min=1, max=3600, help="Token lifetime in seconds (default 300)."
    ),
) -> None:
    """Mint a short-TTL, multi-use capability token that downloads EVERY
    attachment of a task/note, and print a ready ``curl -o`` per file.

    Hand these to an agent or machine WITHOUT Mycelium credentials: each curl
    carries the ephemeral token in the Authorization header, no PAT and no
    workspace id. The token is scoped to that parent's attachments and
    expires after ``--ttl`` (default 300s); it is multi-use until then, so
    all the curls share it. The operator's own credential is used only to
    mint and never leaves this machine.
    """
    kind = parent_kind.strip().lower()
    if kind not in ("task", "note"):
        raise CLIError("parent_kind must be 'task' or 'note'.")
    with client() as c:
        full = _resolve_parent(c, kind, parent_id)
        res = get_json(
            c.post(
                "/attachments/capability",
                json={"parent_kind": kind, "parent_id": full, "ttl_seconds": ttl_seconds},
            )
        )
        base = str(c.base_url).rstrip("/")
    auth = f"Bearer {res['token']}"
    attachments = res.get("attachments", []) if isinstance(res, dict) else []
    for a in attachments:
        url = f"{base}/attachments/{a['id']}/download"
        a["curl"] = f"curl -fsS '{url}' -H 'Authorization: {auth}' -o {shlex.quote(a['filename'])}"
    if json_mode():
        emit_json(res)
        return
    success(
        f"minted attachment:read on {kind} {short_id(full)} — "
        f"{len(attachments)} file(s), expires {res['expires_at']}"
    )
    if not attachments:
        warn("this parent has no attachments; the token is valid but downloads nothing.")
        return
    for a in attachments:
        typer.echo(a["curl"])
