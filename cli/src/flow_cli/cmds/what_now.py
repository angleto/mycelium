"""``flow what-now`` — advisor: feasible tasks given a window and location."""

from __future__ import annotations

import datetime as dt
from typing import Any

import typer

from flow_cli.cmds._common import client, get_json, short_id
from flow_cli.ui import emit_json, emit_table, info, json_mode

_NECESSITIES = ("must", "should", "could")


def what_now(
    duration: int = typer.Option(
        30, "--duration", "-d", min=1, help="Max minutes available from now."
    ),
    location: str | None = typer.Option(None, "--location", "-l"),
    context: list[str] = typer.Option(
        [], "--context", "-c", help="Capability context tag (repeatable, the ctx: gate)."
    ),
    focus_tag: list[str] = typer.Option(
        [], "--focus-tag", help="Project/client tag id to scope by (repeatable)."
    ),
    tag: list[str] = typer.Option([], "--tag", help="Generic tag id to select by (repeatable)."),
    min_priority: int | None = typer.Option(
        None,
        "--min-priority",
        min=1,
        max=25,
        help="Importance floor: keep tasks at least this important (priority <= N; 1=top..25).",
    ),
    min_necessity: str | None = typer.Option(
        None, "--min-necessity", help="Necessity floor: must|should|could."
    ),
    narrate: bool = typer.Option(
        False, "--narrate", help="Also ask the advisor for an AI rationale."
    ),
    start: str | None = typer.Option(
        None, "--start", help="Window start (ISO datetime); defaults to now."
    ),
) -> None:
    """Ask the advisor what's worth doing given the time/place at hand.

    ``--focus-tag`` is a hard SCOPE: when set, only tasks carrying one of
    those tags are considered. ``--min-priority``, ``--min-necessity`` and
    ``--tag`` then combine by UNION within that scope (kept when at least
    one matches), after which feasibility (effort fit, free window,
    dependencies) applies. ``--location`` is a soft, case-insensitive
    substring place filter (tasks with no place are kept). The ranking is
    deterministic; ``--narrate`` only adds prose, it never reorders.
    """
    if min_necessity is not None and min_necessity not in _NECESSITIES:
        raise typer.BadParameter("min-necessity must be one of: must, should, could")
    body: dict[str, object] = {
        "duration_minutes": duration,
        "window_start": _parse_iso(start).isoformat() if start else _now().isoformat(),
        "context_tags": context,
        "narrate": narrate,
    }
    if location:
        body["location"] = location
    if focus_tag:
        body["focus_tag_ids"] = focus_tag
    if tag:
        body["any_tag_ids"] = tag
    if min_priority is not None:
        body["min_priority"] = min_priority
    if min_necessity is not None:
        body["min_necessity"] = min_necessity
    with client() as c:
        resp = get_json(c.post("/advisory/what-now", json=body))
    if json_mode():
        emit_json(resp)
        return
    headers = ["id", "title", "need", "pri", "urgency", "slack(m)", "due", "rem(m)"]

    def _rows(items: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        return [
            (
                short_id(r.get("task_id")),
                _truncate(str(r.get("title", "")), 56),
                r.get("necessity"),
                r.get("priority"),
                r.get("deadline_bucket"),
                _slack(r.get("slack_minutes")),
                r.get("due_date") or "",
                r.get("remaining_minutes"),
            )
            for r in items
        ]

    rows = resp.get("ranked", [])
    if not rows:
        info("[dim]nothing fits that window.[/dim]")
    else:
        emit_table(None, headers, _rows(rows))
    # Tasks that clear every other filter but need more time than the window:
    # shown apart so a too-long overdue/at-risk task is not silently hidden.
    over = resp.get("over_window", [])
    if over:
        emit_table("Needs a longer window (effort exceeds it)", headers, _rows(over))
    if narrate:
        if resp.get("narrated") and resp.get("narration"):
            model = resp.get("narration_model")
            attribution = f" ({model})" if model else ""
            info(f"\n[bold]AI advice[/bold]{attribution}:\n{resp['narration']}")
        else:
            info("[dim]AI advice unavailable in this workspace.[/dim]")


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _parse_iso(s: str) -> dt.datetime:
    d = dt.datetime.fromisoformat(s)
    return d.astimezone() if d.tzinfo is None else d


def _slack(v: object) -> str:
    return "" if v is None else str(v)


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"
