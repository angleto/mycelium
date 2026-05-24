"""``flow what-now`` — advisor: feasible tasks given a window and location."""

from __future__ import annotations

import datetime as dt

import typer

from flow_cli.cmds._common import client, get_json, short_id
from flow_cli.ui import emit_json, emit_table, info, json_mode


def what_now(
    duration: int = typer.Option(30, "--duration", "-d", min=1, help="Available minutes from now."),
    location: str | None = typer.Option(None, "--location", "-l"),
    context: list[str] = typer.Option([], "--context", "-c", help="Context tag (repeatable)."),
    start: str | None = typer.Option(
        None, "--start", help="Window start (ISO datetime); defaults to now."
    ),
) -> None:
    """Ask the advisor what's worth doing given the time/place at hand."""
    payload = {
        "duration_minutes": duration,
        "window_start": _parse_iso(start).isoformat() if start else _now().isoformat(),
        "context_tags": context,
    }
    if location:
        payload["location"] = location
    with client() as c:
        rows = get_json(c.post("/advisory/what-now", json=payload))
    if json_mode():
        emit_json(rows)
        return
    if not rows:
        info("[dim]nothing actionable in that window.[/dim]")
        return
    emit_table(
        None,
        ["id", "title", "need", "pri", "due", "rem(m)"],
        [
            (
                short_id(r.get("task_id")),
                _truncate(r.get("title", ""), 60),
                r.get("necessity"),
                r.get("priority"),
                r.get("due_date") or "",
                r.get("remaining_minutes"),
            )
            for r in rows
        ],
    )


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _parse_iso(s: str) -> dt.datetime:
    d = dt.datetime.fromisoformat(s)
    return d.astimezone() if d.tzinfo is None else d


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"
