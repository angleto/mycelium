"""``mycelium-hook``: the one command every Claude Code hook runs
(docs/adr/0078). It reads the hook's payload on stdin and tells Mycelium
whether the session needs its person.

| hook | what is sent |
|---|---|
| SessionStart | the session is opened; the agent is told its join key |
| UserPromptSubmit, PostToolUse | working |
| Notification permission_prompt | blocked on a permission |
| Notification elicitation_dialog | blocked on a question |
| Stop, StopFailure | your turn; and the machine's live sessions, at most once a minute |
| SessionEnd | the session is closed |
| PreCompact | nothing to the server; the reset shadow records the compaction |

In a project the person allowed, every event also passes through the reset
shadow (``reset_shadow``, docs/adr/0081), which writes a local record and
sends nothing.

Two properties decide its shape.

**It never fails the session it observes.** Every error is written to the
log in the state directory and the exit code is 0. A hook that blocked or
broke Claude Code because Mycelium was unreachable would be uninstalled
within the hour, and rightly.

**It is cheap when there is nothing to say.** ``PostToolUse`` fires on every
tool call. The decision is made with the standard library and a state file;
only a change of state imports the HTTP client, which is most of the CLI's
start-up time (0.7 to 0.9 s against 0.1 s, measured 2026-11-26).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shlex
import subprocess
import sys
import traceback
from typing import Any

from mycelium_cli import agent_host

#: The variable the agent's shell gets the join key in; read by
#: ``mycelium worker open``. Not imported from ``cmds.worker``, which pulls
#: in typer and the HTTP client on the hook's fast path.
ENV_SESSION_KEY = "MYCELIUM_SESSION_KEY"

#: What the hook sends for each event, before reading the payload further.
_WORKING = ("working", None)
_YOUR_TURN = ("your_turn", None)

#: ``notification_type`` values that mean the session cannot go on. Others
#: (auth, quota, idle_prompt, which repeats a Stop already sent) say nothing
#: about whether the person is needed beyond what is already known.
_BLOCKING_NOTIFICATIONS = {
    "permission_prompt": ("blocked", "permission"),
    "elicitation_dialog": ("blocked", "question"),
    "elicitation_url_dialog": ("blocked", "question"),
}


def _log(message: str) -> None:
    try:
        p = agent_host.log_path()
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Kept short: the log is for "why does my session not show", and a
        # line per failure with the time answers it.
        if p.exists() and p.stat().st_size > 256_000:
            p.write_text("", encoding="utf-8")
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {message}\n")
    except OSError:
        pass


def desired(event: str, payload: dict[str, Any]) -> tuple[str, str | None] | None:
    """The attention an event means, or None when it means nothing new."""
    if event in ("UserPromptSubmit", "PostToolUse", "PostToolUseFailure"):
        return _WORKING
    if event in ("Stop", "StopFailure"):
        return _YOUR_TURN
    if event == "Notification":
        return _BLOCKING_NOTIFICATIONS.get(str(payload.get("notification_type") or ""))
    return None


def context_for_agent(session: dict[str, Any], key: str) -> str:
    """What the agent inside the session is told at its start. Short, and it
    names the one action it asks for, because it is read by a model at the
    top of every session."""
    project = session.get("project") or "this project"
    return (
        f"Mycelium has registered this Claude Code session ({project}) so the person can see "
        "from the Mycelium sessions page whether it is waiting for them and what it is "
        "working on. When you open your Mycelium worker, pass "
        f"join_key='{key}' to worker_open so the tasks you hold show on this session's row. "
        "The key grants nothing else. The mycelium CLI in this session's shell joins on its "
        "own. If worker_open refuses the key as unknown, open the worker without it and tell "
        "the person the session is not joined."
    )


def _start(payload: dict[str, Any]) -> int:
    from mycelium_cli import session_sync as sync

    if not sync.logged_in():
        _log("SessionStart: the CLI is not logged in; run `mycelium auth login`")
        return 0
    ref = str(payload.get("session_id") or "")
    cwd = str(payload.get("cwd") or "")
    session, key = sync.open_session(ref, cwd)
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if env_file:
        with open(env_file, "a", encoding="utf-8") as f:
            f.write(f"export {ENV_SESSION_KEY}={shlex.quote(key)}\n")
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context_for_agent(session, key),
            }
        },
        sys.stdout,
    )
    _reconcile_detached()
    return 0


def _reconcile_detached() -> None:
    """Run a reconcile in its own process, so a hook that must answer at
    once (SessionStart injects context, synchronously) does not wait for the
    registry and the server."""
    try:
        subprocess.Popen(
            [sys.executable, "-m", "mycelium_cli.session_hook", "--reconcile"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        _log(f"reconcile: could not start: {exc}")


def _shadow(event: str, payload: dict[str, Any]) -> None:
    """The reset shadow sees every event first. A fault in it is logged and
    goes no further: it must not stop the report of whether the session
    needs its person."""
    try:
        from mycelium_cli import reset_shadow

        reset_shadow.observe(event, payload)
    except Exception as exc:  # the shadow never costs the session its attention report
        _log(f"reset shadow: {type(exc).__name__}: {exc}")


def handle(payload: dict[str, Any]) -> int:
    event = str(payload.get("hook_event_name") or "")
    ref = str(payload.get("session_id") or "")
    if not ref:
        return 0
    _shadow(event, payload)
    if event == "SessionStart":
        return _start(payload)
    if event == "SessionEnd":
        from mycelium_cli import session_sync as sync

        if sync.logged_in():
            sync.close(ref)
        return 0
    want = desired(event, payload)
    if want is None:
        return 0
    state = agent_host.load_state(ref)
    unchanged = state is not None and state.sent == f"{want[0]}:{want[1] or ''}"
    if not unchanged:
        from mycelium_cli import session_sync as sync

        if not sync.logged_in():
            return 0
        sync.report(ref, str(payload.get("cwd") or ""), want[0], want[1])
    if event in ("Stop", "StopFailure"):
        from mycelium_cli import session_sync as sync

        sync.reconcile()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if args == ["--reconcile"]:
            from mycelium_cli import session_sync as sync

            if sync.logged_in():
                sync.reconcile()
            return 0
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return 0
        return handle(payload)
    except Exception as exc:  # the hook never fails the session it observes
        _log(f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
