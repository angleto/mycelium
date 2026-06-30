"""``mycelium today`` — running timer + tasks scheduled for today (or a chosen date)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import typer

from mycelium_cli.cmds._common import client, get_json, short_id
from mycelium_cli.http import CLIError
from mycelium_cli.ui import emit_json, emit_table, info, json_mode


def today(
    date_spec: str | None = typer.Option(
        None,
        "--date",
        "-d",
        help="Day to show (YYYY-MM-DD, 'today', 'tomorrow', or +/-N days). Default: today.",
    ),
    tz: str | None = typer.Option(
        None,
        "--tz",
        help="Override local timezone (e.g. Europe/Rome). Defaults to system.",
    ),
) -> None:
    """Tasks due/scheduled on the chosen day + any currently running timer.

    Includes pending reminders that fire today, and highlights appointments
    (tasks with start_at + duration_minutes).
    """
    target = _parse_date(date_spec, _tz(tz))
    with client() as c:
        running = get_json(c.get("/time/running"))
        tasks = get_json(c.get("/tasks", params={"include_archived": "false"}))

    relevant = [t for t in tasks if _is_on(t, target)]
    relevant.sort(
        key=lambda t: (
            t.get("start_at") or "9999",
            -int(t.get("priority", 3)),
        )
    )

    appointments = [t for t in relevant if t.get("start_at") and t.get("duration_minutes")]
    deadlines = [t for t in relevant if not (t.get("start_at") and t.get("duration_minutes"))]

    if json_mode():
        emit_json(
            {
                "date": target.isoformat(),
                "running": running,
                "appointments": appointments,
                "deadlines": deadlines,
            }
        )
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

    if appointments:
        emit_table(
            f"Appointments ({target.isoformat()})",
            ["id", "title", "when", "pri", "with"],
            [
                (
                    short_id(t.get("id")),
                    _truncate(t.get("title", ""), 60),
                    _appt_when(t),
                    t.get("priority"),
                    t.get("assignee_handle") or "",
                )
                for t in appointments
            ],
        )

    if deadlines:
        emit_table(
            f"Due / scheduled ({target.isoformat()})",
            ["id", "title", "state", "due", "pri"],
            [
                (
                    short_id(t.get("id")),
                    _truncate(t.get("title", ""), 60),
                    t.get("state"),
                    t.get("due_date") or "",
                    t.get("priority"),
                )
                for t in deadlines
            ],
        )

    if not appointments and not deadlines:
        info(f"[dim]nothing scheduled for {target.isoformat()}.[/dim]")


def week(
    start_spec: str | None = typer.Option(
        None,
        "--from",
        help="ISO start date (defaults to today's Monday).",
    ),
    tz: str | None = typer.Option(None, "--tz"),
) -> None:
    """Tasks scheduled in the next 7 days, grouped by day."""
    target_tz = _tz(tz)
    start = _parse_date(start_spec, target_tz) if start_spec else _monday_of(_today(target_tz))
    end = start + dt.timedelta(days=7)
    with client() as c:
        tasks = get_json(c.get("/tasks", params={"include_archived": "false"}))
    in_window = [t for t in tasks if _in_range(t, start, end)]
    by_day: dict[str, list[dict[str, Any]]] = {}
    for t in in_window:
        key = _slot_date(t).isoformat()
        by_day.setdefault(key, []).append(t)
    for tasks_today in by_day.values():
        tasks_today.sort(key=lambda t: (t.get("start_at") or "9999", -int(t.get("priority", 3))))

    if json_mode():
        emit_json({"from": start.isoformat(), "to": end.isoformat(), "by_day": by_day})
        return

    for offset in range(7):
        day = start + dt.timedelta(days=offset)
        label = f"{day.strftime('%a')} {day.isoformat()}"
        rows = by_day.get(day.isoformat(), [])
        if not rows:
            info(f"[bold]{label}[/bold] [dim]— empty[/dim]")
            continue
        emit_table(
            label,
            ["id", "title", "state", "when", "pri"],
            [
                (
                    short_id(t.get("id")),
                    _truncate(t.get("title", ""), 60),
                    t.get("state"),
                    _appt_when(t) or (t.get("due_date") or ""),
                    t.get("priority"),
                )
                for t in rows
            ],
        )


# --- helpers --------------------------------------------------------


def _tz(name: str | None) -> dt.tzinfo:
    if not name:
        return dt.datetime.now().astimezone().tzinfo or dt.UTC
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as exc:
        raise CLIError(f"unknown timezone '{name}'.") from exc


def _today(tz: dt.tzinfo) -> dt.date:
    return dt.datetime.now(tz).date()


def _parse_date(spec: str | None, tz: dt.tzinfo) -> dt.date:
    if not spec or spec == "today":
        return _today(tz)
    if spec == "tomorrow":
        return _today(tz) + dt.timedelta(days=1)
    if spec.startswith(("+", "-")):
        try:
            delta = int(spec)
        except ValueError as exc:
            raise CLIError(f"invalid --date '{spec}'.") from exc
        return _today(tz) + dt.timedelta(days=delta)
    try:
        return dt.date.fromisoformat(spec)
    except ValueError as exc:
        raise CLIError(f"invalid --date '{spec}'.") from exc


def _monday_of(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def _is_on(task: dict[str, Any], day: dt.date) -> bool:
    due = task.get("due_date")
    if isinstance(due, str) and due[:10] == day.isoformat():
        return True
    start = task.get("start_at")
    if isinstance(start, str) and start[:10] == day.isoformat():
        return True
    return False


def _in_range(task: dict[str, Any], start: dt.date, end: dt.date) -> bool:
    due = task.get("due_date")
    if isinstance(due, str):
        try:
            if start <= dt.date.fromisoformat(due[:10]) < end:
                return True
        except ValueError:
            pass
    s = task.get("start_at")
    if isinstance(s, str):
        try:
            if start <= dt.date.fromisoformat(s[:10]) < end:
                return True
        except ValueError:
            pass
    return False


def _slot_date(task: dict[str, Any]) -> dt.date:
    s = task.get("start_at")
    if isinstance(s, str):
        try:
            return dt.date.fromisoformat(s[:10])
        except ValueError:
            pass
    due = task.get("due_date")
    if isinstance(due, str):
        try:
            return dt.date.fromisoformat(due[:10])
        except ValueError:
            pass
    return dt.date.today()


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
