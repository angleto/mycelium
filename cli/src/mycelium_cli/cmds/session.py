"""``mycelium sessions`` and ``mycelium session ...``: which of your agent
sessions needs you, and the hooks that tell Mycelium (docs/adr/0078).

``mycelium sessions`` answers "which one needs me" for every session of the
workspace, on every machine, and takes you to the tmux pane of the one you
pick on this machine. It reads three sources, in this order of authority:

1. Claude Code's registry of the sessions alive here, the only source that
   knows a session is at a prompt right now;
2. the state files the hooks wrote, which say which Mycelium session each
   local session opened;
3. the server, which knows the tasks each session's agent holds and the
   sessions on other machines.

Where the registry and the server disagree about a session on this machine,
the registry is the fresher of the two (``Stop`` does not fire on an
interrupt, so the server can be left at "working"), and the view sends the
correction before it reads the page. The page is then drawn in the server's
order, so the CLI and the web never disagree about who comes first.

``mycelium session install-hooks`` prints the Claude Code settings that run
``mycelium-hook`` on the session's events, and with ``--apply`` merges them
into the settings file, keeping a copy of the previous one.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Group as RichGroup
from rich.live import Live
from rich.table import Table
from rich.text import Text

from mycelium_cli import agent_host
from mycelium_cli import session_sync as sync
from mycelium_cli.cmds._common import client, get_json
from mycelium_cli.http import CLIError
from mycelium_cli.ui import emit_json, err, json_mode, out

app = typer.Typer(
    no_args_is_help=True,
    help="The hooks that tell Mycelium whether an agent session needs you.",
)

#: The keys a row can be picked with, in order: digits first, the hand is
#: already there.
_KEYS = "123456789abcdefghijklmnopqrstuvwxyz"

_GROUP_TITLES = {
    "needs_you": "NEEDS YOU",
    "working": "WORKING",
    "quiet": "GONE QUIET",
}


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------


def duration(seconds: float) -> str:
    """How long, the way a person says it at a glance: 40 s, 12 min, 3 h,
    2 d. Coarser as it grows, because "3 h 12 min" is a number nobody acts
    on differently from "3 h"."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h"
    return f"{s // 86400} d"


def state_words(row: dict[str, Any], now: dt.datetime) -> str:
    """The state of a row in words, with how long it has been so."""
    since = row.get("attention_since")
    for_ = f" · {duration((now - _ts(since)).total_seconds())}" if since else ""
    attention = row.get("attention")
    if attention == "blocked":
        what = (
            "waiting for a permission"
            if row.get("blocked_on") == "permission"
            else "waiting for an answer"
        )
        return what + for_
    if attention == "your_turn":
        return "finished, your turn" + for_
    heard = f"{duration((now - _ts(row['heard_at'])).total_seconds())}"
    if row.get("group") == "quiet":
        return f"no news for {heard}"
    if attention == "working":
        return "working" + for_
    return f"not reporting · seen {heard} ago"


def _ts(raw: str) -> dt.datetime:
    return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _name(row: dict[str, Any]) -> str:
    return row.get("project") or row.get("title") or row.get("label") or row.get("machine") or "?"


# ---------------------------------------------------------------------------
# Gathering
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class View:
    """What is drawn: the server's rows in its order, each with the pane it
    is in when it runs on this machine, and what could not be read."""

    rows: list[dict[str, Any]]
    now: dt.datetime
    panes: dict[str, agent_host.Pane] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    local_only: bool = False


def _correct(
    live: list[agent_host.LiveSession], states: dict[str, agent_host.HookState], notes: list[str]
) -> None:
    """Bring the server in line with the registry for this machine: open
    the live sessions the hooks never opened (hooks installed after they
    started), send the state the registry sees where it differs from what
    was last sent, and close what is no longer alive."""
    for s in live:
        want = agent_host.attention_of(s)
        st = states.get(s.host_ref)
        if st is not None and st.sent == f"{want[0]}:{want[1] or ''}":
            continue
        try:
            sync.report(s.host_ref, s.cwd, want[0], want[1], title=s.name)
        except CLIError as exc:
            notes.append(f"could not update {agent_host.project_name(s.cwd)}: {exc}")
    try:
        sync.reconcile(force=True, live=live)
    except CLIError as exc:
        notes.append(f"could not tell the server which sessions are alive: {exc}")


def gather(*, correct: bool = True) -> View:
    notes: list[str] = []
    try:
        live = agent_host.live_sessions()
    except agent_host.HostRegistryError as exc:
        live = []
        notes.append(f"this machine's sessions could not be read: {exc}")
    panes_by_ref = agent_host.panes_of(live)
    logged_in = sync.logged_in()
    if correct and logged_in and live:
        _correct(live, agent_host.all_states(), notes)
    states = agent_host.all_states()
    by_session = {st.session_id: st.host_ref for st in states.values()}
    if logged_in:
        try:
            with client(timeout=10.0) as c:
                page = get_json(c.get("/sessions"))
            panes = {
                str(r["session_id"]): panes_by_ref[by_session[str(r["session_id"])]]
                for r in page["sessions"]
                if r.get("session_id")
                and str(r["session_id"]) in by_session
                and by_session[str(r["session_id"])] in panes_by_ref
            }
            return View(rows=page["sessions"], now=_ts(page["now"]), panes=panes, notes=notes)
        except CLIError as exc:
            notes.append(f"Mycelium could not be reached ({exc}); showing this machine only")
    else:
        notes.append("not logged in to Mycelium (mycelium auth login); showing this machine only")
    return _local_view(live, panes_by_ref, notes)


def _local_view(
    live: list[agent_host.LiveSession],
    panes_by_ref: dict[str, agent_host.Pane],
    notes: list[str],
) -> View:
    """This machine's sessions from the registry alone, when the server
    cannot be asked. Ordered blocked, your turn, working: the server's order
    without the durations it measures, which nothing here can."""
    rank = {"blocked": 0, "your_turn": 1, "working": 2}
    rows: list[dict[str, Any]] = []
    panes: dict[str, agent_host.Pane] = {}
    for s in live:
        attention, blocked_on = agent_host.attention_of(s)
        key = f"local:{s.host_ref}"
        rows.append(
            {
                "session_id": key,
                "project": agent_host.project_name(s.cwd),
                "title": s.name,
                "machine": agent_host.machine_label(),
                "attention": attention,
                "blocked_on": blocked_on,
                "attention_since": None,
                "heard_at": dt.datetime.now(dt.UTC).isoformat(),
                "group": "working" if attention == "working" else "needs_you",
                "held": [],
            }
        )
        if s.host_ref in panes_by_ref:
            panes[key] = panes_by_ref[s.host_ref]
    rows.sort(key=lambda r: rank[r["attention"]])
    return View(rows=rows, now=dt.datetime.now(dt.UTC), panes=panes, notes=notes, local_only=True)


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def _where(row: dict[str, Any], view: View, *, wide: bool = True) -> str:
    pane = view.panes.get(str(row.get("session_id")))
    if pane is not None:
        return f"{pane.target} {pane.window_name}".strip() if wide else pane.target
    machine = row.get("machine")
    here = agent_host.machine_label()
    if machine and machine != here:
        return f"on {machine}"
    return ""


def render(view: View, *, keys: bool) -> RichGroup:
    """One table for every group, so the columns line up down the screen,
    with each group's title as a section row. Widths in the order a person
    reads: who, then whether it needs them, then on what, then where; the
    key column never shrinks, because a row without its key cannot be
    picked."""
    parts: list[Any] = []
    if not view.rows:
        parts.append(
            Text(
                "No agent sessions are open. Run `mycelium session install-hooks` to have "
                "your Claude Code sessions show here.",
                style="dim",
            )
        )
    # Below 100 columns the window name goes and the name narrows: the
    # widths then fit 80 columns, so the key column is never the one
    # squeezed out (measured at 80 columns, where it was).
    wide = out().width >= 100
    t = Table(box=None, show_header=False, pad_edge=False, expand=False, padding=(0, 2, 0, 0))
    if keys:
        t.add_column(min_width=1, max_width=1, no_wrap=True, style="bold cyan")
    t.add_column(
        min_width=12, max_width=24 if wide else 16, no_wrap=True, overflow="ellipsis", style="bold"
    )
    t.add_column(min_width=26, no_wrap=True, overflow="ellipsis")
    t.add_column(min_width=10, max_width=48, no_wrap=True, overflow="ellipsis")
    t.add_column(
        min_width=8, max_width=28 if wide else 16, no_wrap=True, overflow="ellipsis", style="dim"
    )
    index = 0
    width = 5 if keys else 4
    for group in ("needs_you", "working", "quiet"):
        rows = [r for r in view.rows if r.get("group") == group]
        if not rows:
            continue
        style = {"needs_you": "bold yellow", "working": "bold", "quiet": "bold dim"}[group]
        title = Text(f"{_GROUP_TITLES[group]} ({len(rows)})", style=style)
        if index:
            t.add_row(*([""] * width))
        t.add_row(*([""] if keys else []), title, *([""] * (width - (2 if keys else 1))))
        for r in rows:
            attention = r.get("attention")
            word_style = {"blocked": "yellow", "your_turn": "green"}.get(attention or "", "")
            if r.get("group") == "quiet":
                word_style = "dim"
            held = ", ".join(h["title"] for h in r.get("held") or [])
            cells: list[Any] = []
            if keys:
                cells.append(_KEYS[index] if index < len(_KEYS) else "")
            cells += [
                _name(r),
                Text(state_words(r, view.now), style=word_style),
                Text(held or "-", style="" if held else "dim"),
                _where(r, view, wide=wide),
            ]
            t.add_row(*cells)
            index += 1
    if view.rows:
        parts.append(t)
    for n in view.notes:
        parts.append(Text(n, style="dim italic"))
    return RichGroup(*parts)


def _pickable(view: View) -> list[dict[str, Any]]:
    return [r for g in ("needs_you", "working", "quiet") for r in view.rows if r.get("group") == g]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def sessions(
    pick: Annotated[
        bool,
        typer.Option(
            "--pick",
            "-p",
            help="Press a session's key to switch tmux to it. Made for a popup: "
            'bind S display-popup -E "mycelium sessions --pick"',
        ),
    ] = False,
    watch: Annotated[
        float | None,
        typer.Option(
            "--watch", "-w", help="Redraw every N seconds, for a pane kept open beside the work."
        ),
    ] = None,
) -> None:
    """Which of your agent sessions needs you, on every machine, and what
    each is working on."""
    if json_mode():
        view = gather()
        emit_json(
            [
                {
                    **r,
                    "tmux": (
                        view.panes[str(r["session_id"])].target
                        if str(r.get("session_id")) in view.panes
                        else None
                    ),
                }
                for r in view.rows
            ]
        )
        return
    if watch is not None:
        interval = max(2.0, watch)
        with Live(render(gather(), keys=False), console=out(), auto_refresh=False) as live:
            try:
                while True:
                    time.sleep(interval)
                    live.update(render(gather(), keys=False), refresh=True)
            except KeyboardInterrupt:
                return
    view = gather()
    out().print(render(view, keys=pick))
    if not pick:
        return
    rows = _pickable(view)
    if not rows:
        typer.getchar()
        return
    err().print("[dim]press a key to go to that session, any other key to close[/dim]")
    ch = typer.getchar()
    if ch not in _KEYS[: len(rows)]:
        return
    row = rows[_KEYS.index(ch)]
    pane = view.panes.get(str(row.get("session_id")))
    if pane is None:
        where = row.get("machine") or "another machine"
        err().print(f"[yellow]{_name(row)} is not in a tmux pane here ({where})[/yellow]")
        typer.getchar()
        return
    agent_host.switch_to(pane)


@app.command("hook", hidden=True)
def hook() -> None:
    """What every Claude Code hook runs (installed as ``mycelium-hook``,
    which starts faster). Reads the hook's payload on stdin."""
    from mycelium_cli.session_hook import main

    raise typer.Exit(main([]))


# ---------------------------------------------------------------------------
# install-hooks
# ---------------------------------------------------------------------------

#: The events and how each hook runs. SessionStart is synchronous because it
#: hands the agent its join key in the session's context, which an async hook
#: cannot; SessionEnd because the process is exiting and an async child can
#: be killed with it. Every other event is async: it must never make the
#: person wait.
_EVENTS: tuple[tuple[str, bool, int], ...] = (
    ("SessionStart", False, 10),
    ("UserPromptSubmit", True, 15),
    ("PostToolUse", True, 15),
    ("Notification", True, 15),
    ("Stop", True, 30),
    ("StopFailure", True, 15),
    ("SessionEnd", False, 5),
)

_MARK = "mycelium-hook"


def _hook_command() -> str:
    found = shutil.which(_MARK)
    if found:
        return found
    beside = Path(sys.argv[0]).resolve().parent / _MARK
    return str(beside) if beside.exists() else _MARK


def hooks_block(command: str) -> dict[str, list[dict[str, Any]]]:
    block: dict[str, list[dict[str, Any]]] = {}
    for event, is_async, timeout in _EVENTS:
        h: dict[str, Any] = {"type": "command", "command": command, "timeout": timeout}
        if is_async:
            h["async"] = True
        block[event] = [{"hooks": [h]}]
    return block


def merge_hooks(settings: dict[str, Any], command: str) -> list[str]:
    """Add the hook to every event that does not run it yet; return the
    events it was added to. Hooks already there, the person's or another
    tool's, are left exactly as they are."""
    hooks = settings.setdefault("hooks", {})
    added: list[str] = []
    for event, groups in hooks_block(command).items():
        current = hooks.setdefault(event, [])
        present = any(
            _MARK in str(h.get("command", ""))
            for g in current
            if isinstance(g, dict)
            for h in g.get("hooks", [])
            if isinstance(h, dict)
        )
        if not present:
            current.extend(groups)
            added.append(event)
    return added


_TMUX_BINDING = 'bind-key S display-popup -E -w 90% -h 60% "mycelium sessions --pick"'


@app.command("install-hooks")
def install_hooks(
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Merge the hooks into the settings file, keeping a copy of the previous one.",
        ),
    ] = False,
    settings_path: Annotated[
        Path,
        typer.Option("--settings", help="The Claude Code settings file to merge into."),
    ] = Path("~/.claude/settings.json"),
) -> None:
    """Make your Claude Code sessions show in Mycelium: the hooks that report
    whether a session needs you, and a tmux key to jump to the one that does.

    Without --apply it prints what to add and changes nothing."""
    command = _hook_command()
    path = settings_path.expanduser()
    if not apply:
        out().print(f"Add to {path} (or run again with --apply):\n")
        out().print_json(json.dumps({"hooks": hooks_block(command)}))
        out().print(
            f"\nAnd to ~/.tmux.conf, for a popup with every session:\n\n  {_TMUX_BINDING}\n"
        )
        return
    try:
        settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except json.JSONDecodeError as exc:
        raise CLIError(f"{path} is not valid JSON ({exc}); nothing was changed.") from exc
    if not isinstance(settings, dict):
        raise CLIError(f"{path} does not hold a JSON object; nothing was changed.")
    added = merge_hooks(settings, command)
    if not added:
        out().print(f"The hooks are already in {path}; nothing changed.")
    else:
        if path.exists():
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = path.with_name(f"{path.name}.bak-{stamp}")
            shutil.copy2(path, backup)
            out().print(f"Previous settings kept in {backup}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        out().print(f"Added the Mycelium hook to {', '.join(added)} in {path}.")
        out().print(
            "Sessions started from now on report themselves; running ones show up the first "
            "time you run `mycelium sessions`."
        )
    out().print(f"\nFor a popup with every session, add to ~/.tmux.conf:\n\n  {_TMUX_BINDING}\n")
