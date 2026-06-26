"""``mycelium open`` — open the SPA on the given resource (browser fallback)."""

from __future__ import annotations

import typer

from mycelium_cli.cmds._common import active_profile, client, resolve_id
from mycelium_cli.http import CLIError
from mycelium_cli.ui import info


def open_url(
    ref: str = typer.Argument(
        ...,
        help="What to open: 'today', 'tasks', 'notes', 'invoices', or a task / note UUID prefix.",
    ),
    print_only: bool = typer.Option(
        False, "--print", help="Print the URL instead of opening a browser."
    ),
) -> None:
    """Open the SPA on a Mycelium resource. Useful when a CLI surface is
    deliberately not built (invoicing) or when you want a visual view."""
    _, prof, _ = active_profile()
    base = prof.base_url.rstrip("/")

    # Static views.
    static = {
        "today": "/tasks?focus=today",
        "tasks": "/tasks",
        "notes": "/notes",
        "garden": "/garden",
        "invoices": "/invoices",
        "calendar": "/calendar",
        "settings": "/settings",
    }
    if ref in static:
        url = f"{base}{static[ref]}"
    else:
        # Try resolving as a task or note prefix.
        url = _resolve_resource_url(ref, base)

    if print_only:
        info(url)
        return
    import webbrowser

    if not webbrowser.open(url):
        # Headless tmux session, ssh, ... → still print.
        info(url)


def _resolve_resource_url(ref: str, base: str) -> str:
    with client() as c:
        # Prefer task first (more common); fall back to note.
        try:
            tid = resolve_id(c, ref, endpoint="/tasks", kind="task")
            return f"{base}/tasks/{tid}"
        except CLIError:
            pass
        nid = resolve_id(c, ref, endpoint="/notes", kind="note")
        return f"{base}/notes/{nid}"
