"""``flow timer`` — start, stop, status (thin wrapper over /time)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import typer

from flow_cli.cmds._common import client, short_id
from flow_cli.cmds.tasks import _resolve_task
from flow_cli.http import raise_for_response
from flow_cli.ui import emit_json, emit_table, info, json_mode, success

app = typer.Typer(no_args_is_help=True, help="Time tracking: start/stop/status.")


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
        entry = _get_json(c.post("/time/start", json=payload))
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
        stopped = _get_json(c.post("/time/stop", json=payload))
    if json_mode():
        emit_json(stopped)
        return
    success("timer stopped")


@app.command()
def status() -> None:
    """List all currently running timers (one per parallel slot)."""
    with client() as c:
        running = _get_json(c.get("/time/running"))
    if json_mode():
        emit_json(running)
        return
    if not running:
        info("[dim]no running timer.[/dim]")
        return
    now = dt.datetime.now(dt.UTC)
    rows: list[tuple[Any, ...]] = []
    for r in running:
        started = _parse_dt(r.get("started_at"))
        elapsed = (now - started).total_seconds() if started else 0
        rows.append(
            (
                short_id(r.get("task_id")),
                _fmt_elapsed(int(elapsed)),
                r.get("memo") or "",
            )
        )
    emit_table("Running timers", ["task_id", "elapsed", "memo"], rows)


def _fmt_elapsed(secs: int) -> str:
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m:02d}m{s:02d}s"


def _parse_dt(s: Any) -> dt.datetime | None:
    if isinstance(s, str):
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _get_json(resp: Any) -> Any:
    raise_for_response(resp)
    return resp.json()
