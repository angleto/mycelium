"""``flow today`` — running timer + tasks scheduled for today."""

from __future__ import annotations

import datetime as dt
from typing import Any

import typer

from flow_cli.cmds._common import client, short_id
from flow_cli.http import raise_for_response
from flow_cli.ui import emit_json, emit_table, info, json_mode


def today(
    tz: str | None = typer.Option(
        None,
        "--tz",
        help="Override the local timezone used to compute 'today' (defaults to system).",
    ),
) -> None:
    """Tasks due/scheduled today and any currently running timer."""
    today_local = dt.datetime.now(_tz(tz)).date()
    with client() as c:
        running = _get_json(c.get("/time/running"))
        tasks = _get_json(c.get("/tasks", params={"include_archived": "false"}))

    relevant = [t for t in tasks if _is_today(t, today_local)]
    relevant.sort(
        key=lambda t: (
            t.get("start_at") or "9999",
            -int(t.get("priority", 3)),
        )
    )

    if json_mode():
        emit_json({"today": today_local.isoformat(), "running": running, "tasks": relevant})
        return

    if running:
        emit_table(
            "Running",
            ["task_id", "started_at", "memo"],
            [
                (short_id(r.get("task_id")), r.get("started_at"), r.get("memo") or "")
                for r in running
            ],
        )
    else:
        info("[dim]no running timer.[/dim]")

    if relevant:
        emit_table(
            f"Today ({today_local.isoformat()})",
            ["id", "title", "state", "due", "when", "pri"],
            [
                (
                    short_id(t.get("id")),
                    _truncate(t.get("title", ""), 60),
                    t.get("state"),
                    t.get("due_date") or "",
                    _appt_when(t),
                    t.get("priority"),
                )
                for t in relevant
            ],
        )
    else:
        info("[dim]nothing scheduled for today.[/dim]")


def _tz(name: str | None) -> dt.tzinfo:
    if not name:
        return dt.datetime.now().astimezone().tzinfo or dt.UTC
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return dt.UTC


def _is_today(task: dict[str, Any], today_local: dt.date) -> bool:
    due = task.get("due_date")
    if isinstance(due, str) and due[:10] == today_local.isoformat():
        return True
    start = task.get("start_at")
    if isinstance(start, str) and start[:10] == today_local.isoformat():
        return True
    return False


def _appt_when(task: dict[str, Any]) -> str:
    start = task.get("start_at")
    dur = task.get("duration_minutes")
    if isinstance(start, str) and dur:
        try:
            d = dt.datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone()
            return f"{d.strftime('%H:%M')} ({dur}m)"
        except ValueError:
            return start
    return ""


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _get_json(resp: Any) -> Any:
    raise_for_response(resp)
    return resp.json()
