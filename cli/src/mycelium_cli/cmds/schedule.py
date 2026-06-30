"""``mycelium schedule`` — view the AI-computed schedule of upcoming tasks."""

from __future__ import annotations

import typer

from mycelium_cli.cmds._common import client, get_json, short_id
from mycelium_cli.ui import emit_json, emit_table, info, json_mode

app = typer.Typer(no_args_is_help=True, help="AI scheduler view (read-only).")


@app.command("list")
def list_(
    project: str | None = typer.Option(None, "--project", help="Filter by project tag id."),
) -> None:
    """Show scheduled tasks with their planned start/end."""
    params: dict[str, str] = {}
    if project:
        params["project_tag_id"] = project
    with client() as c:
        rows = get_json(c.get("/schedule", params=params))
    if json_mode():
        emit_json(rows)
        return
    if not rows:
        info("[dim]nothing scheduled.[/dim]")
        return
    emit_table(
        None,
        ["task", "start", "end", "assignee"],
        [
            (
                short_id(r.get("task_id")),
                str(r.get("start_at") or "")[:16],
                str(r.get("end_at") or "")[:16],
                r.get("assignee_handle") or "",
            )
            for r in rows
        ],
    )


@app.command()
def recompute(
    policy: str = typer.Option(
        "balanced", "--policy", help="balanced | fastest | cheapest | throughput."
    ),
    project: str | None = typer.Option(None, "--project"),
) -> None:
    """Trigger a re-plan of the schedule (owner-gated)."""
    payload: dict[str, str] = {"policy": policy}
    if project:
        payload["project_tag_id"] = project
    with client(role="owner") as c:
        result = get_json(c.post("/schedule/recompute", json=payload))
    if json_mode():
        emit_json(result)
        return
    from mycelium_cli.ui import success

    success(
        f"recomputed: {result.get('count')} tasks, "
        f"makespan {result.get('makespan_minutes')}m, "
        f"unassignable {result.get('unassignable_count')}"
    )
