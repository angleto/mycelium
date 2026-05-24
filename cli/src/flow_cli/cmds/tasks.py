"""``flow task`` — list, show, add, done, verify, edit."""

from __future__ import annotations

import datetime as dt
from typing import Any

import typer

from flow_cli.cmds._common import client, short_id
from flow_cli.http import CLIError, raise_for_response
from flow_cli.ui import edit_in_editor, emit_json, emit_table, info, json_mode, out, success

app = typer.Typer(no_args_is_help=True, help="Tasks: list, show, create, transition.")


@app.command("list")
def list_(
    state: str | None = typer.Option(
        None, "--state", "-s", help="Filter by state name (e.g. todo, in_progress, done)."
    ),
    tag: str | None = typer.Option(None, "--tag", "-t", help="Filter by tag name."),
    archived: bool = typer.Option(
        False, "--archived/--no-archived", help="Include archived tasks."
    ),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=500),
) -> None:
    """List tasks in the active workspace."""
    with client() as c:
        rows = _get_json(c.get("/tasks", params={"include_archived": str(archived).lower()}))
        if tag:
            tag_id = _resolve_tag(c, tag)
            rows = [
                t for t in rows if any(str(tg.get("id")) == tag_id for tg in t.get("tags") or [])
            ]
        if state:
            rows = [t for t in rows if str(t.get("state")) == state]
    rows = rows[:limit]
    _render_tasks(rows)


@app.command()
def show(
    task_id: str = typer.Argument(
        ..., help="Task UUID (short prefixes accepted if unique among listed)."
    ),
) -> None:
    """Show one task with its description and tags."""
    with client() as c:
        full = _resolve_task(c, task_id)
        task = _get_json(c.get(f"/tasks/{full}"))
    if json_mode():
        emit_json(task)
        return
    out().print(f"[bold]{task['title']}[/bold]  [dim]({task['state']})[/dim]")
    out().print(f"id: {task['id']}  pri: {task['priority']}  ver: {task['version']}")
    if task.get("due_date"):
        out().print(f"due: {task['due_date']}")
    if task.get("start_at"):
        out().print(f"start: {task['start_at']}  ({task.get('duration_minutes', '?')}m)")
    if task.get("tags"):
        out().print("tags: " + ", ".join(t.get("name", "") for t in task["tags"]))
    if task.get("description"):
        out().print("\n" + task["description"])


@app.command()
def add(
    title: str = typer.Argument(..., help="Task title."),
    description: str | None = typer.Option(
        None,
        "--description",
        "-d",
        help="Task description. Use '-' to read from stdin; omit to open $EDITOR.",
    ),
    due: str | None = typer.Option(
        None, "--due", help="Due date (YYYY-MM-DD or 'today'/'tomorrow')."
    ),
    priority: int = typer.Option(3, "--priority", "-p", min=1, max=25),
    no_editor: bool = typer.Option(
        False, "--no-editor", help="Do not open $EDITOR even if description is empty."
    ),
) -> None:
    """Create a task. With no -d/--description, opens $EDITOR for the body."""
    if description == "-":
        import sys as _sys

        description = _sys.stdin.read().strip() or None
    elif description is None and not no_editor:
        body_text = edit_in_editor(f"# {title}\n\n").strip()
        description = body_text or None
    payload: dict[str, Any] = {"title": title, "priority": priority}
    if description:
        payload["description"] = description
    due_date = _parse_due(due)
    if due_date:
        payload["due_date"] = due_date.isoformat()
    with client() as c:
        created = _get_json(c.post("/tasks", json=payload))
    if json_mode():
        emit_json(created)
        return
    success(f"Created task [bold]{short_id(created['id'])}[/bold] — {created['title']}")


@app.command()
def done(
    task_id: str = typer.Argument(..., help="Task to mark as done."),
) -> None:
    """Transition the task to the 'done' state."""
    _transition(task_id, target_name="done")


@app.command()
def verify(
    task_id: str = typer.Argument(..., help="Task to mark as awaiting verification."),
) -> None:
    """Transition the task to the 'verify' state (pre-done, user gate)."""
    _transition(task_id, target_name="verify")


@app.command()
def to(
    task_id: str = typer.Argument(...),
    state: str = typer.Argument(..., help="Target state name."),
) -> None:
    """Generic transition: ``flow task to <id> in_progress``."""
    _transition(task_id, target_name=state)


def _transition(task_id: str, *, target_name: str) -> None:
    with client() as c:
        full = _resolve_task(c, task_id)
        task = _get_json(c.get(f"/tasks/{full}"))
        states = _get_json(c.get(f"/tasks/{full}/states"))
        target = next((s for s in states if str(s.get("name")) == target_name), None)
        if target is None:
            available = ", ".join(str(s.get("name")) for s in states)
            raise CLIError(f"No reachable state '{target_name}'. Available: {available}")
        if str(target["id"]) == str(task["state_id"]):
            info(f"already in state '{target_name}'.")
            return
        _get_json(
            c.post(
                f"/tasks/{full}/state",
                json={"state_id": target["id"], "expected_version": task["version"]},
            )
        )
    success(f"task {short_id(task_id)} → [bold]{target_name}[/bold]")


def _render_tasks(rows: list[dict[str, Any]]) -> None:
    if json_mode():
        emit_json(rows)
        return
    emit_table(
        None,
        ["id", "title", "state", "due", "pri", "tags"],
        [
            (
                short_id(t.get("id")),
                _truncate(t.get("title", ""), 60),
                t.get("state"),
                t.get("due_date") or "",
                t.get("priority"),
                ",".join(tg.get("name", "") for tg in t.get("tags") or []),
            )
            for t in rows
        ],
    )


def _resolve_task(c: Any, partial: str) -> str:
    """Accept either a full UUID or a unique short prefix (against /tasks)."""
    if len(partial) >= 32:
        return partial
    rows = _get_json(c.get("/tasks", params={"include_archived": "true"}))
    matches = [t for t in rows if str(t.get("id", "")).startswith(partial)]
    if len(matches) == 1:
        return str(matches[0]["id"])
    if not matches:
        raise CLIError(f"No task matches '{partial}'.")
    raise CLIError(
        f"Ambiguous task prefix '{partial}' ({len(matches)} matches). Use more characters."
    )


def _resolve_tag(c: Any, name_or_id: str) -> str:
    if len(name_or_id) >= 32:
        return name_or_id
    rows = _get_json(c.get("/tags"))
    matches = [t for t in rows if str(t.get("name")).lower() == name_or_id.lower()]
    if not matches:
        raise CLIError(f"No tag named '{name_or_id}'.")
    return str(matches[0]["id"])


def _parse_due(spec: str | None) -> dt.date | None:
    if not spec:
        return None
    today = dt.date.today()
    if spec == "today":
        return today
    if spec == "tomorrow":
        return today + dt.timedelta(days=1)
    try:
        return dt.date.fromisoformat(spec)
    except ValueError as exc:
        raise CLIError(f"Invalid --due '{spec}'. Use YYYY-MM-DD, 'today', or 'tomorrow'.") from exc


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _get_json(resp: Any) -> Any:
    raise_for_response(resp)
    return resp.json()
