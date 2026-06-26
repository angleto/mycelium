"""``mycelium tag`` — list tags (filter by kind)."""

from __future__ import annotations

from typing import Any

import typer

from mycelium_cli.cmds._common import client
from mycelium_cli.http import raise_for_response
from mycelium_cli.ui import emit_json, emit_table, json_mode

app = typer.Typer(no_args_is_help=True, help="Tags taxonomy.")


@app.command("list")
def list_(
    kind: str | None = typer.Option(
        None, "--kind", "-k", help="Filter by kind (e.g. context, area, client, project)."
    ),
    include_archived: bool = typer.Option(False, "--archived/--no-archived"),
) -> None:
    """List taxonomy tags."""
    params: dict[str, str] = {"include_archived": str(include_archived).lower()}
    if kind:
        params["kind"] = kind
    with client() as c:
        rows = _get_json(c.get("/tags", params=params))
    if json_mode():
        emit_json(rows)
        return
    emit_table(
        None,
        ["id", "kind", "name", "status"],
        [(r.get("id", "")[:8], r.get("kind"), r.get("name"), r.get("status")) for r in rows],
    )


def _get_json(resp: Any) -> Any:
    raise_for_response(resp)
    return resp.json()
