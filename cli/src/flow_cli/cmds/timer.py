"""``flow timer`` — start, stop, status, entry add/edit/rm."""

from __future__ import annotations

import datetime as dt
from typing import Any

import typer

from flow_cli.cmds._common import client, get_json, resolve_id, short_id
from flow_cli.http import CLIError
from flow_cli.ui import emit_json, emit_table, info, json_mode, success

app = typer.Typer(no_args_is_help=True, help="Time tracking: start/stop/status + entry CRUD.")


def _resolve_task(c: Any, partial: str) -> str:
    return resolve_id(c, partial, endpoint="/tasks", kind="task")


@app.command()
def start(
    task_id: str = typer.Argument(..., help="Task to bill the running entry to."),
    memo: str | None = typer.Option(None, "--memo", "-m"),
    parallel: bool = typer.Option(False, "--parallel", help="Run alongside other timers."),
    billable: bool | None = typer.Option(None, "--billable/--unbillable"),
) -> None:
    """Start a timer on a task."""
    payload: dict[str, Any] = {"parallel": parallel}
    if memo:
        payload["memo"] = memo
    if billable is not None:
        payload["billable"] = billable
    with client() as c:
        payload["task_id"] = _resolve_task(c, task_id)
        entry = get_json(c.post("/time/start", json=payload))
    if json_mode():
        emit_json(entry)
        return
    success(f"timer started on [bold]{short_id(payload['task_id'])}[/bold]")


@app.command()
def stop(
    task_id: str | None = typer.Argument(None, help="Task to stop (omit for the serial timer)."),
    memo: str | None = typer.Option(None, "--memo", "-m"),
) -> None:
    """Stop a running timer."""
    payload: dict[str, Any] = {}
    if memo:
        payload["memo"] = memo
    with client() as c:
        if task_id is not None:
            payload["task_id"] = _resolve_task(c, task_id)
        stopped = get_json(c.post("/time/stop", json=payload))
    if json_mode():
        emit_json(stopped)
        return
    success("timer stopped")


@app.command()
def status() -> None:
    """List running timers + today's total billable time."""
    today_local = dt.date.today()
    with client() as c:
        running = get_json(c.get("/time/running"))
        # Today's entries to compute the cumulative count: clipped to
        # local-day boundary so the running interval still contributes.
        params = {
            "start_from": dt.datetime.combine(today_local, dt.time(0, 0)).isoformat(),
            "start_to": dt.datetime.combine(today_local, dt.time(23, 59, 59)).isoformat(),
        }
        entries = get_json(c.get("/time/entries", params=params))

    if json_mode():
        emit_json({"running": running, "today_entries": entries})
        return

    now = dt.datetime.now(dt.UTC)
    if running:
        rows = []
        for r in running:
            started = _parse_dt(r.get("started_at"))
            elapsed = int((now - started).total_seconds()) if started else 0
            rows.append((short_id(r.get("task_id")), _fmt_elapsed(elapsed), r.get("memo") or ""))
        emit_table("Running", ["task_id", "elapsed", "memo"], rows)
    else:
        info("[dim]no running timer.[/dim]")

    total_secs = sum(int(e.get("duration_seconds") or 0) for e in entries)
    # Add the running portion (not yet stopped → no duration_seconds).
    for r in running:
        started = _parse_dt(r.get("started_at"))
        if started:
            total_secs += int((now - started).total_seconds())
    if total_secs:
        info(f"\n[bold]today total[/bold]: {_fmt_elapsed(total_secs)} ({len(entries)} entries)")


# --- entry CRUD -----------------------------------------------------

entry_app = typer.Typer(no_args_is_help=True, help="Manual time entries (add / edit / delete).")
app.add_typer(entry_app, name="entry")


@entry_app.command("add")
def entry_add(
    task_id: str = typer.Argument(..., help="Task this entry bills to."),
    start: str = typer.Option(..., "--start", help="ISO start datetime (e.g. 2026-05-24T09:00)."),
    end: str | None = typer.Option(None, "--end", help="ISO end datetime."),
    duration_minutes: int | None = typer.Option(
        None, "--duration", "-d", min=1, help="Alternative to --end."
    ),
    memo: str | None = typer.Option(None, "--memo", "-m"),
    billable: bool | None = typer.Option(None, "--billable/--unbillable"),
) -> None:
    """Create a manual time entry (e.g. forgot to start the timer)."""
    if not end and not duration_minutes:
        raise CLIError("provide either --end or --duration.")
    started_at = _parse_iso(start)
    payload: dict[str, Any] = {"started_at": started_at.isoformat()}
    if end:
        payload["ended_at"] = _parse_iso(end).isoformat()
    if duration_minutes:
        payload["duration_seconds"] = duration_minutes * 60
    if memo:
        payload["memo"] = memo
    if billable is not None:
        payload["billable"] = billable
    with client() as c:
        payload["task_id"] = _resolve_task(c, task_id)
        entry = get_json(c.post("/time/entries", json=payload))
    if json_mode():
        emit_json(entry)
        return
    success(f"entry {short_id(entry.get('id'))} created.")


@entry_app.command("list")
def entry_list(
    task: str | None = typer.Option(None, "--task", help="Filter by task id."),
    since: str | None = typer.Option(None, "--since", help="ISO start datetime (inclusive)."),
    until: str | None = typer.Option(None, "--until", help="ISO end datetime (exclusive)."),
    limit: int = typer.Option(30, "--limit", "-n", min=1, max=500),
) -> None:
    """List recent time entries."""
    params: dict[str, str] = {"limit": str(limit)}
    if since:
        params["start_from"] = _parse_iso(since).isoformat()
    if until:
        params["start_to"] = _parse_iso(until).isoformat()
    with client() as c:
        if task:
            params["task_id"] = _resolve_task(c, task)
        rows = get_json(c.get("/time/entries", params=params))
    if json_mode():
        emit_json(rows)
        return
    if not rows:
        info("[dim]no entries.[/dim]")
        return
    emit_table(
        None,
        ["id", "task", "started", "duration", "memo"],
        [
            (
                short_id(r.get("id")),
                short_id(r.get("task_id")),
                str(r.get("started_at") or "")[:16],
                _fmt_elapsed(int(r.get("duration_seconds") or 0)),
                r.get("memo") or "",
            )
            for r in rows
        ],
    )


@entry_app.command("edit")
def entry_edit(
    entry_id: str = typer.Argument(..., help="Entry UUID (full)."),
    task: str | None = typer.Option(None, "--task"),
    memo: str | None = typer.Option(None, "--memo", "-m"),
    billable: bool | None = typer.Option(None, "--billable/--unbillable"),
    start: str | None = typer.Option(None, "--start"),
    end: str | None = typer.Option(None, "--end"),
) -> None:
    """Patch a time entry. Only fields you pass change."""
    with client() as c:
        current = get_json(c.get(f"/time/entries/{entry_id}"))
        payload: dict[str, Any] = {"expected_version": current["version"]}
        if task is not None:
            payload["task_id"] = _resolve_task(c, task)
        if memo is not None:
            payload["memo"] = memo
        if billable is not None:
            payload["billable"] = billable
        if start is not None:
            payload["started_at"] = _parse_iso(start).isoformat()
        if end is not None:
            payload["ended_at"] = _parse_iso(end).isoformat()
        result = get_json(c.patch(f"/time/entries/{entry_id}", json=payload))
    if json_mode():
        emit_json(result)
        return
    success(f"entry {short_id(entry_id)} updated (v{result.get('version')})")


@entry_app.command("rm")
def entry_rm(entry_id: str = typer.Argument(...)) -> None:
    """Delete a time entry."""
    with client() as c:
        resp = c.delete(f"/time/entries/{entry_id}")
        if resp.status_code not in (200, 204):
            get_json(resp)
    success(f"entry {short_id(entry_id)} deleted.")


# --- internals ------------------------------------------------------


def _fmt_elapsed(secs: int) -> str:
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m:02d}m{s:02d}s"


def _parse_dt(s: Any) -> dt.datetime | None:
    if isinstance(s, str):
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _parse_iso(s: str) -> dt.datetime:
    try:
        # Accept compact form like "2026-05-24T09:00" by letting fromisoformat
        # add the missing seconds; assume local tz if naive.
        out = dt.datetime.fromisoformat(s)
    except ValueError as exc:
        raise CLIError(f"invalid ISO datetime '{s}'.") from exc
    if out.tzinfo is None:
        out = out.astimezone()
    return out
