"""``mycelium task`` — list, show, add, edit, transition, tag, comment, attach, remind."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import typer

from mycelium_cli.cmds._common import (
    attachment_markdown_ref,
    client,
    get_json,
    resolve_id,
    short_id,
)
from mycelium_cli.completion import complete_task_id
from mycelium_cli.http import CLIError
from mycelium_cli.ui import (
    body_or_none,
    edit_in_editor,
    emit_json,
    emit_table,
    info,
    json_mode,
    out,
    success,
)

app = typer.Typer(no_args_is_help=True, help="Tasks: list, show, create, edit, transition.")


# Terminal states are hidden from the default `mycelium task list` (and from
# the today view). We can't fetch the full workflow per call so the
# heuristic is name-based: any of these is considered "closed".
_TERMINAL_NAMES = {"done", "verified", "cancelled", "archived", "rejected"}

# Default sort: due first (nulls last), then priority desc, then most
# recently updated. Stable so paging looks predictable.
_SORT_KEYS = {
    "due": lambda t: (
        t.get("due_date") or "9999-12-31",
        -int(t.get("priority") or 0),
        t.get("updated_at") or "",
    ),
    "priority": lambda t: (-int(t.get("priority") or 0), t.get("due_date") or "9999-12-31"),
    "updated": lambda t: (t.get("updated_at") or "",),
    "title": lambda t: (str(t.get("title") or "").lower(),),
}


def _resolve_task(c: Any, partial: str) -> str:
    return resolve_id(c, partial, endpoint="/tasks", kind="task")


def _resolve_tag(c: Any, name_or_id: str) -> str:
    """Kind-blind resolution, for the surfaces where any kind is a legal
    answer: the list filter and ``task tag add/rm`` (attaching a project
    tag there is a MOVE, ADR-0050, not a mistake to catch here)."""
    if len(name_or_id) >= 32:
        return name_or_id
    rows = get_json(c.get("/tags"))
    matches = [t for t in rows if str(t.get("name")).lower() == name_or_id.lower()]
    if not matches:
        raise CLIError(f"no tag named '{name_or_id}'.")
    return str(matches[0]["id"])


# The kinds ``--tag`` accepts on create. A task's client and project are
# structural (ADR-0050: exactly one of each) and have their own
# single-valued flags, so a repeated ``-t`` can no longer name a second
# client and be silently accepted.
_FREEFORM_KINDS = ("generic", "memory_channel")
_STRUCTURAL_HINT = "A task's client and project are structural: pass them as --client / --project."


def _tag_index(c: Any) -> list[dict[str, Any]]:
    """The whole tag vocabulary in one call, resolved once per command
    instead of once per ``--tag``. Archived tags are included on
    purpose: archiving hides a tag from the pickers, it never stops it
    from being attached."""
    return list(get_json(c.get("/tags", params={"include_archived": "true"})))


def _pick_tag(
    rows: list[dict[str, Any]], name_or_id: str, *, kinds: tuple[str, ...], flag: str
) -> str:
    """Resolve one tag name (case-insensitive) or UUID within ``kinds``.

    Names are unique per (org, kind), so the kind is part of the lookup
    key, not a post-hoc check: '--project Acme' and '--client Acme' can
    legitimately be two different tags.
    """
    needle = name_or_id.strip().lower()
    matches = [
        t
        for t in rows
        if str(t.get("id", "")).lower() == needle or str(t.get("name", "")).lower() == needle
    ]
    wanted = [t for t in matches if str(t.get("kind", "")) in kinds]
    if not wanted:
        if matches:
            raise CLIError(
                f"{flag}: '{name_or_id}' is a {matches[0].get('kind')} tag, not {'/'.join(kinds)}.",
                hint=_STRUCTURAL_HINT,
            )
        raise CLIError(f"{flag}: no {'/'.join(kinds)} tag named '{name_or_id}'.")
    if len(wanted) > 1:
        raise CLIError(
            f"{flag}: '{name_or_id}' matches {len(wanted)} tags.",
            hint="Pass the tag UUID instead.",
        )
    return str(wanted[0]["id"])


@app.command("list")
def list_(
    state: str | None = typer.Option(
        None, "--state", "-s", help="Filter by state name (e.g. todo, in_progress, done)."
    ),
    tag: str | None = typer.Option(None, "--tag", "-t", help="Filter by tag name or UUID."),
    archived: bool = typer.Option(
        False, "--archived/--no-archived", help="Include archived tasks."
    ),
    all_states: bool = typer.Option(
        False,
        "--all",
        help="Include terminal states (done/verified/...); off by default.",
    ),
    sort: str = typer.Option(
        "due", "--sort", help="Sort key: due | priority | updated | title.", show_default=True
    ),
    reverse: bool = typer.Option(False, "--reverse", "-r", help="Reverse the sort order."),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=500),
) -> None:
    """List tasks in the active workspace (open tasks only by default)."""
    params: dict[str, str] = {"include_archived": str(archived).lower()}
    if state:
        # When a state name is requested we still need its UUID; tag/state
        # query goes server-side via tag_id when possible.
        pass
    with client() as c:
        if tag:
            params["tag_id"] = _resolve_tag(c, tag)
        rows = get_json(c.get("/tasks", params=params))
        if state:
            rows = [t for t in rows if str(t.get("state")) == state]
        elif not all_states:
            rows = [t for t in rows if str(t.get("state")) not in _TERMINAL_NAMES]
    keyfn = _SORT_KEYS.get(sort)
    if keyfn is None:
        raise CLIError(f"unknown --sort '{sort}'. Valid: {', '.join(_SORT_KEYS)}.")
    rows.sort(key=keyfn, reverse=reverse)
    rows = rows[:limit]
    _render_tasks(rows)


@app.command()
def show(
    task_id: str = typer.Argument(
        ...,
        autocompletion=complete_task_id,
        help="Task UUID (short prefixes accepted if unique).",
    ),
) -> None:
    """Show one task with its description, tags, comments and reminders."""
    with client() as c:
        full = _resolve_task(c, task_id)
        task = get_json(c.get(f"/tasks/{full}"))
        comments = get_json(c.get(f"/tasks/{full}/comments"))
        reminders = get_json(c.get(f"/tasks/{full}/reminders"))
    if json_mode():
        emit_json({"task": task, "comments": comments, "reminders": reminders})
        return
    out().print(f"[bold]{task['title']}[/bold]  [dim]({task['state']})[/dim]")
    out().print(
        f"id: {task['id']}  pri: {task['priority']}  ver: {task['version']}"
        f"  assignee: {task.get('assignee_handle') or '-'}"
    )
    if task.get("due_date"):
        out().print(f"due: {task['due_date']}")
    if task.get("start_at"):
        out().print(f"start: {task['start_at']}  ({task.get('duration_minutes', '?')}m)")
    if task.get("tags"):
        out().print("tags: " + ", ".join(t.get("name", "") for t in task["tags"]))
    if task.get("description"):
        out().print("\n" + task["description"])
    if reminders:
        out().print("\n[bold]reminders[/bold]")
        for r in reminders:
            out().print(f"  -{r.get('offset_minutes')}m  id={short_id(r.get('id'))}")
    if comments:
        out().print("\n[bold]comments[/bold]")
        for cm in comments:
            out().print(f"  [{short_id(cm.get('id'))}] {cm.get('body')}")


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
    importance: int | None = typer.Option(
        None,
        "--importance",
        "-I",
        min=1,
        max=5,
        help="Eisenhower importance (1..5, 1=Critical). Default Low (4).",
    ),
    urgency: int | None = typer.Option(
        None,
        "--urgency",
        "-U",
        min=1,
        max=5,
        help="Eisenhower urgency (1..5, 1=Now). Default Low (4).",
    ),
    client_tag: str | None = typer.Option(
        None,
        "--client",
        help="Client tag name or UUID. Optional: the project decides the client, "
        "so this only asserts the one you expect.",
    ),
    project_tag: str | None = typer.Option(
        None,
        "--project",
        help="Project tag name or UUID. Omitted = the workspace default project.",
    ),
    tag: list[str] = typer.Option(
        [], "--tag", "-t", help="Generic tag name or UUID; pass multiple times."
    ),
    no_editor: bool = typer.Option(
        False, "--no-editor", help="Do not open $EDITOR even if description is empty."
    ),
) -> None:
    """Create a task. With no -d/--description, opens $EDITOR for the body."""
    if description == "-":
        import sys as _sys

        description = body_or_none(_sys.stdin.read())
    elif description is None and not no_editor:
        description = body_or_none(edit_in_editor(f"# {title}\n\n"))
    # ``priority`` is intentionally not a CLI option: it is a calculated
    # field (importance x urgency, clamped 1..25). Pass --importance /
    # --urgency instead; defaults to Low/Low (4/4 -> priority 16) at the
    # backend when omitted.
    payload: dict[str, Any] = {"title": title}
    if importance is not None:
        payload["importance"] = importance
    if urgency is not None:
        payload["urgency"] = urgency
    if description:
        payload["description"] = description
    due_date = _parse_due(due)
    if due_date:
        payload["due_date"] = due_date.isoformat()
    with client() as c:
        if client_tag or project_tag or tag:
            rows = _tag_index(c)
            ids: list[str] = []
            if client_tag:
                ids.append(_pick_tag(rows, client_tag, kinds=("client",), flag="--client"))
            if project_tag:
                ids.append(_pick_tag(rows, project_tag, kinds=("project",), flag="--project"))
            ids += [_pick_tag(rows, t, kinds=_FREEFORM_KINDS, flag="--tag") for t in tag]
            # POST /tasks takes one flat bag; the server sorts it by kind
            # in tag_assignment.resolve_structural. So the structural
            # pair rides WITH the create instead of being attached
            # afterwards, and a --client contradicting --project is
            # refused before any row exists (ADR-0050).
            payload["tag_ids"] = ids
        created = get_json(c.post("/tasks", json=payload))
    if json_mode():
        emit_json(created)
        return
    success(f"Created task [bold]{short_id(created['id'])}[/bold] — {created['title']}")


@app.command()
def edit(
    task_id: str = typer.Argument(..., autocompletion=complete_task_id),
    title: str | None = typer.Option(None, "--title"),
    description: str | None = typer.Option(
        None,
        "--description",
        "-d",
        help="New description. '-' = stdin; '@' = open $EDITOR pre-loaded with current body.",
    ),
    due: str | None = typer.Option(
        None, "--due", help="YYYY-MM-DD, 'today', 'tomorrow', or '-' to clear."
    ),
    importance: int | None = typer.Option(None, "--importance", "-I", min=1, max=5),
    urgency: int | None = typer.Option(None, "--urgency", "-U", min=1, max=5),
    billable: bool | None = typer.Option(None, "--billable/--unbillable"),
    location: str | None = typer.Option(None, "--location"),
) -> None:
    """Patch task fields. Only fields you pass are changed. ``priority``
    is a calculated field (importance x urgency); patch the axes."""
    with client() as c:
        full = _resolve_task(c, task_id)
        current = get_json(c.get(f"/tasks/{full}"))
        payload: dict[str, Any] = {"expected_version": current["version"]}
        if title is not None:
            payload["title"] = title
        if description == "-":
            import sys as _sys

            payload["description"] = _sys.stdin.read()
        elif description == "@":
            payload["description"] = edit_in_editor(current.get("description") or "")
        elif description is not None:
            payload["description"] = description
        if due == "-":
            payload["due_date"] = None
        elif due is not None:
            d = _parse_due(due)
            payload["due_date"] = d.isoformat() if d else None
        for key, val in (
            ("importance", importance),
            ("urgency", urgency),
            ("billable", billable),
            ("location", location),
        ):
            if val is not None:
                payload[key] = val
        result = get_json(c.patch(f"/tasks/{full}", json=payload))
    if json_mode():
        emit_json(result)
        return
    success(f"updated task {short_id(full)} (v{result.get('version')})")


@app.command()
def done(
    task_id: str = typer.Argument(
        ..., autocompletion=complete_task_id, help="Task to mark as done."
    ),
) -> None:
    """Transition the task to the 'done' state."""
    _transition(task_id, target_name="done")


@app.command()
def verify(
    task_id: str = typer.Argument(
        ..., autocompletion=complete_task_id, help="Task to mark as awaiting verification."
    ),
) -> None:
    """Transition the task to the 'verify' state (pre-done, user gate)."""
    _transition(task_id, target_name="verify")


@app.command()
def to(
    task_id: str = typer.Argument(..., autocompletion=complete_task_id),
    state: str = typer.Argument(..., help="Target state name."),
) -> None:
    """Generic transition: ``mycelium task to <id> in_progress``."""
    _transition(task_id, target_name=state)


@app.command()
def archive(task_id: str = typer.Argument(..., autocompletion=complete_task_id)) -> None:
    """Archive (soft-hide) a task."""
    _versioned_action(task_id, "archive")


@app.command()
def unarchive(task_id: str = typer.Argument(..., autocompletion=complete_task_id)) -> None:
    """Restore an archived task to the active list."""
    _versioned_action(task_id, "unarchive")


@app.command("delete")
def delete_(task_id: str = typer.Argument(..., autocompletion=complete_task_id)) -> None:
    """Soft-delete a task (recoverable from trash)."""
    _versioned_action(task_id, "delete")


@app.command()
def restore(task_id: str = typer.Argument(..., autocompletion=complete_task_id)) -> None:
    """Restore a soft-deleted task."""
    _versioned_action(task_id, "restore")


# --- tag / comment / remind / attach sub-groups -----------------------

tag_app = typer.Typer(no_args_is_help=True, help="Add/remove tags on a task.")
app.add_typer(tag_app, name="tag")

comment_app = typer.Typer(no_args_is_help=True, help="Comments on a task.")
app.add_typer(comment_app, name="comment")

remind_app = typer.Typer(no_args_is_help=True, help="Reminders on a task.")
app.add_typer(remind_app, name="remind")

attach_app = typer.Typer(no_args_is_help=True, help="Attachments on a task.")
app.add_typer(attach_app, name="attach")

desc_app = typer.Typer(
    no_args_is_help=True,
    help="Partial writes on a task's description (append/prepend) without resending the body.",
)
app.add_typer(desc_app, name="desc")


@tag_app.command("add")
def tag_add(
    task_id: str = typer.Argument(..., autocompletion=complete_task_id),
    tag: str = typer.Argument(...),
) -> None:
    """Attach a tag (by name or UUID) to a task."""
    with client() as c:
        full = _resolve_task(c, task_id)
        tag_id = _resolve_tag(c, tag)
        resp = c.post(f"/tasks/{full}/tags", json={"tag_id": tag_id})
        if resp.status_code not in (200, 204):
            get_json(resp)
    success(f"tagged {short_id(full)} with '{tag}'")


@tag_app.command("rm")
def tag_rm(
    task_id: str = typer.Argument(..., autocompletion=complete_task_id),
    tag: str = typer.Argument(...),
) -> None:
    """Detach a tag from a task."""
    with client() as c:
        full = _resolve_task(c, task_id)
        tag_id = _resolve_tag(c, tag)
        resp = c.delete(f"/tasks/{full}/tags/{tag_id}")
        if resp.status_code not in (200, 204):
            get_json(resp)
    success(f"detached '{tag}' from {short_id(full)}")


@comment_app.command("add")
def comment_add(
    task_id: str = typer.Argument(..., autocompletion=complete_task_id),
    body: str | None = typer.Option(
        None, "--body", "-m", help="Comment body. Use '-' for stdin; omit to open $EDITOR."
    ),
) -> None:
    """Post a comment on a task."""
    if body == "-":
        import sys as _sys

        body = _sys.stdin.read()
    elif body is None:
        body = edit_in_editor("")
    body = body_or_none(body)
    if body is None:
        raise CLIError("empty comment body, aborting.")
    with client() as c:
        full = _resolve_task(c, task_id)
        cm = get_json(c.post(f"/tasks/{full}/comments", json={"body": body}))
    if json_mode():
        emit_json(cm)
        return
    success(f"comment {short_id(cm.get('id'))} added on {short_id(full)}")


@comment_app.command("list")
def comment_list(task_id: str = typer.Argument(..., autocompletion=complete_task_id)) -> None:
    """List comments on a task."""
    with client() as c:
        full = _resolve_task(c, task_id)
        rows = get_json(c.get(f"/tasks/{full}/comments"))
    if json_mode():
        emit_json(rows)
        return
    if not rows:
        info("[dim]no comments.[/dim]")
        return
    emit_table(
        None,
        ["id", "author", "body"],
        [
            (short_id(r.get("id")), short_id(r.get("author_identity_id")), r.get("body"))
            for r in rows
        ],
    )


@comment_app.command("resolve")
def comment_resolve(
    annotation_id: str = typer.Argument(..., help="Comment id (full UUID)."),
) -> None:
    """Mark a task comment resolved."""
    with client() as c:
        version = int(get_json(c.get(f"/annotations/{annotation_id}"))["version"])
        get_json(
            c.post(f"/annotations/{annotation_id}/resolve", json={"expected_version": version})
        )
    success(f"resolved comment {short_id(annotation_id)}")


@remind_app.command("add")
def remind_add(
    task_id: str = typer.Argument(..., autocompletion=complete_task_id),
    offset_minutes: int = typer.Argument(
        ..., help="Minutes before due_date to fire (e.g. 60 = 1h, 1440 = 1 day)."
    ),
) -> None:
    """Add a pre-due reminder."""
    with client() as c:
        full = _resolve_task(c, task_id)
        r = get_json(c.post(f"/tasks/{full}/reminders", json={"offset_minutes": offset_minutes}))
    if json_mode():
        emit_json(r)
        return
    success(f"reminder {short_id(r.get('id'))} set -{offset_minutes}m on {short_id(full)}")


@remind_app.command("rm")
def remind_rm(
    task_id: str = typer.Argument(..., autocompletion=complete_task_id),
    reminder_id: str = typer.Argument(..., help="Reminder UUID (full or unique short prefix)."),
) -> None:
    """Remove a reminder."""
    with client() as c:
        full = _resolve_task(c, task_id)
        # Reminders live under a nested collection, so we cannot use the
        # generic ``resolve_id`` helper (which assumes a top-level list
        # endpoint). Do the prefix match against the task's own reminder
        # list instead.
        rem_full = reminder_id
        if len(reminder_id) < 32:
            rows = get_json(c.get(f"/tasks/{full}/reminders"))
            matches = [r for r in rows if str(r.get("id", "")).startswith(reminder_id)]
            if len(matches) == 1:
                rem_full = str(matches[0]["id"])
            elif not matches:
                raise CLIError(f"no reminder matches '{reminder_id}' on this task.")
            else:
                raise CLIError(
                    f"ambiguous reminder prefix '{reminder_id}' "
                    f"({len(matches)} matches). Use more characters."
                )
        resp = c.delete(f"/tasks/{full}/reminders/{rem_full}")
        if resp.status_code not in (200, 204):
            get_json(resp)
    success(f"reminder {short_id(reminder_id)} removed.")


@attach_app.command("add")
def attach_add(
    task_id: str = typer.Argument(..., autocompletion=complete_task_id),
    path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
) -> None:
    """Upload a file attachment to a task."""
    with client() as c:
        full = _resolve_task(c, task_id)
        with path.open("rb") as fh:
            files = {"file": (path.name, fh, _guess_mime(path))}
            res = get_json(c.post(f"/tasks/{full}/attachments", files=files))
    ref = attachment_markdown_ref(res)
    if isinstance(res, dict):
        res["markdown_ref"] = ref
    if json_mode():
        emit_json(res)
        return
    success(f"uploaded '{path.name}' to task {short_id(full)}")
    typer.echo(f"markdown: {ref}")


@attach_app.command("list")
def attach_list(task_id: str = typer.Argument(..., autocompletion=complete_task_id)) -> None:
    """List attachments on a task."""
    with client() as c:
        full = _resolve_task(c, task_id)
        rows = get_json(c.get(f"/tasks/{full}/attachments"))
    if json_mode():
        emit_json(rows)
        return
    emit_table(
        None,
        ["id", "filename", "size", "mime"],
        [
            (
                short_id(r.get("id")),
                r.get("filename"),
                r.get("size_bytes"),
                r.get("mime_type"),
            )
            for r in rows
        ],
    )


@desc_app.command("append")
def desc_append(
    task_id: str = typer.Argument(..., autocompletion=complete_task_id),
    text: str | None = typer.Option(
        None, "--text", "-m", help="Text to append. Use '-' for stdin; omit to open $EDITOR."
    ),
    separator: str = typer.Option(
        "\n\n", "--separator", help="Inserted between the old body and the new text."
    ),
) -> None:
    """Append text to the END of a task's description without resending
    the body (task 5662a07f). Joined to the existing body by --separator."""
    text = _read_body_text(text)
    with client() as c:
        full = _resolve_task(c, task_id)
        current = get_json(c.get(f"/tasks/{full}"))
        resp = get_json(
            c.post(
                f"/tasks/{full}/description/append",
                json={
                    "text": text,
                    "separator": separator,
                    "expected_version": current["version"],
                },
            )
        )
    if json_mode():
        emit_json(resp)
        return
    success(
        f"appended {resp.get('appended_chars')} chars to task {short_id(full)} "
        f"description (v{resp.get('version')})"
    )


@desc_app.command("prepend")
def desc_prepend(
    task_id: str = typer.Argument(..., autocompletion=complete_task_id),
    text: str | None = typer.Option(
        None, "--text", "-m", help="Text to prepend. Use '-' for stdin; omit to open $EDITOR."
    ),
    separator: str = typer.Option(
        "\n\n", "--separator", help="Inserted between the new text and the old body."
    ),
) -> None:
    """Prepend text to the FRONT of a task's description without resending
    the body (task 5662a07f). Joined to the existing body by --separator."""
    text = _read_body_text(text)
    with client() as c:
        full = _resolve_task(c, task_id)
        current = get_json(c.get(f"/tasks/{full}"))
        resp = get_json(
            c.post(
                f"/tasks/{full}/description/prepend",
                json={
                    "text": text,
                    "separator": separator,
                    "expected_version": current["version"],
                },
            )
        )
    if json_mode():
        emit_json(resp)
        return
    success(
        f"prepended {resp.get('appended_chars')} chars to task {short_id(full)} "
        f"description (v{resp.get('version')})"
    )


# --- graph -----------------------------------------------------------


@app.command()
def graph(
    task_id: str | None = typer.Argument(
        None,
        autocompletion=complete_task_id,
        help="Focus on one task (predecessors/successors). Omit for the whole graph.",
    ),
    project_tag_id: str | None = typer.Option(
        None, "--project", help="Restrict the whole-graph view to one project."
    ),
) -> None:
    """Show task dependencies as an ASCII tree.

    With an argument: predecessors above the task, successors below.
    Without: the full graph as an adjacency listing (cheap rendering;
    use the SPA for a real diagram).
    """
    with client() as c:
        if task_id:
            full = _resolve_task(c, task_id)
            task = get_json(c.get(f"/tasks/{full}"))
            deps = get_json(c.get("/dependencies", params={"task_id": full}))
            _render_task_graph(task, deps, full)
        else:
            params: dict[str, str] = {}
            if project_tag_id:
                params["project_tag_id"] = project_tag_id
            g = get_json(c.get("/graph", params=params))
            _render_full_graph(g)


def _render_task_graph(task: dict[str, Any], deps: list[dict[str, Any]], focus_id: str) -> None:
    if json_mode():
        emit_json({"task": task, "dependencies": deps})
        return
    predecessors = [d for d in deps if str(d.get("successor_id")) == focus_id]
    successors = [d for d in deps if str(d.get("predecessor_id")) == focus_id]
    title = task.get("title", "?")
    state = task.get("state", "?")

    out().print("")
    if predecessors:
        out().print("[bold]Depends on[/bold]")
        for d in predecessors:
            kind = d.get("type", "?")
            lag = d.get("lag_working_minutes") or 0
            tag = f" [{kind}{f' +{lag}m' if lag else ''}]"
            out().print(f"  ├── {short_id(d.get('predecessor_id'))}{tag}")
        out().print("  │")
    out().print(f"  [bold]● {short_id(focus_id)}[/bold]  {title}  [dim]({state})[/dim]")
    if successors:
        out().print("  │")
        out().print("[bold]Blocks[/bold]")
        for d in successors:
            kind = d.get("type", "?")
            lag = d.get("lag_working_minutes") or 0
            tag = f" [{kind}{f' +{lag}m' if lag else ''}]"
            out().print(f"  └── {short_id(d.get('successor_id'))}{tag}")
    if not predecessors and not successors:
        info("[dim]no dependencies.[/dim]")


def _render_full_graph(graph_data: dict[str, Any]) -> None:
    if json_mode():
        emit_json(graph_data)
        return
    nodes = {str(n.get("id")): n for n in graph_data.get("nodes") or []}
    edges = graph_data.get("edges") or []
    if not nodes:
        info("[dim]no tasks in graph.[/dim]")
        return
    by_pred: dict[str, list[dict[str, Any]]] = {}
    has_pred: set[str] = set()
    for e in edges:
        by_pred.setdefault(str(e.get("predecessor")), []).append(e)
        has_pred.add(str(e.get("successor")))
    # Roots = nodes with no incoming edges; we render each subtree from
    # there. Anything left over (cycles or orphan) is appended.
    roots = sorted(n for n in nodes if n not in has_pred)
    rendered: set[str] = set()

    def _walk(node_id: str, prefix: str, is_last: bool) -> None:
        rendered.add(node_id)
        node = nodes.get(node_id) or {}
        connector = "└── " if is_last else "├── "
        flag = " [red](blocked)[/red]" if node.get("blocked") else ""
        out().print(
            f"{prefix}{connector}{short_id(node_id)}  "
            f"{_truncate(node.get('title', ''), 60)}  [dim]({node.get('state', '?')})[/dim]{flag}"
        )
        children = by_pred.get(node_id) or []
        next_prefix = prefix + ("    " if is_last else "│   ")
        for i, e in enumerate(children):
            child = str(e.get("successor"))
            if child in rendered:
                out().print(f"{next_prefix}└── [dim]→ {short_id(child)} (cycle)[/dim]")
                continue
            _walk(child, next_prefix, i == len(children) - 1)

    for i, root in enumerate(roots):
        _walk(root, "", i == len(roots) - 1)
    leftover = [n for n in nodes if n not in rendered]
    if leftover:
        out().print("\n[bold]Cycles / unreachable[/bold]")
        for nid in sorted(leftover):
            node = nodes.get(nid) or {}
            out().print(
                f"  {short_id(nid)}  {_truncate(node.get('title', ''), 60)}  "
                f"[dim]({node.get('state', '?')})[/dim]"
            )


# --- internals -------------------------------------------------------


def _transition(task_id: str, *, target_name: str) -> None:
    with client() as c:
        full = _resolve_task(c, task_id)
        task = get_json(c.get(f"/tasks/{full}"))
        states = get_json(c.get(f"/tasks/{full}/states"))
        target = next((s for s in states if str(s.get("name")) == target_name), None)
        if target is None:
            available = ", ".join(str(s.get("name")) for s in states)
            raise CLIError(f"no reachable state '{target_name}'. Available: {available}")
        if str(target["id"]) == str(task["state_id"]):
            info(f"already in state '{target_name}'.")
            return
        get_json(
            c.post(
                f"/tasks/{full}/state",
                json={"state_id": target["id"], "expected_version": task["version"]},
            )
        )
    success(f"task {short_id(task_id)} → [bold]{target_name}[/bold]")


def _versioned_action(task_id: str, action: str) -> None:
    with client() as c:
        full = _resolve_task(c, task_id)
        current = get_json(c.get(f"/tasks/{full}"))
        get_json(
            c.post(
                f"/tasks/{full}/{action}",
                json={"expected_version": current["version"]},
            )
        )
    success(f"task {short_id(full)} {action}d")


def _render_tasks(rows: list[dict[str, Any]]) -> None:
    if json_mode():
        emit_json(rows)
        return
    if not rows:
        info("[dim]no tasks.[/dim]")
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


def _read_body_text(text: str | None) -> str:
    """Resolve a ``--text`` option to a non-empty string: ``-`` reads
    stdin, ``None`` opens $EDITOR, anything else is taken literally.
    Raises ``CLIError`` on empty input."""
    if text == "-":
        import sys as _sys

        text = _sys.stdin.read()
    elif text is None:
        text = edit_in_editor("")
    if not text:
        raise CLIError("empty text, aborting.")
    return text


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
        raise CLIError(f"invalid --due '{spec}'. Use YYYY-MM-DD, 'today', or 'tomorrow'.") from exc


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _guess_mime(path: Path) -> str:
    import mimetypes

    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"
