"""The calls an agent host's hooks make to Mycelium (docs/adr/0078), and the
one repair they share.

Imported only when a hook has something to say: this module pulls in the
HTTP client, which is most of the CLI's start-up time. ``session_hook``
decides first, with the standard library alone, whether there is anything to
send.

The repair: a session the server closed (its machine stopped listing it, or
it was silent for days) is refused with ``session.not_found`` on a report.
The hook opens it again under the same host id, which the server answers
with the same row, and sends the report again. The agent's worker stays
joined, because it joined that row.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mycelium_cli import agent_host
from mycelium_cli.cmds._common import active_profile, client, get_json
from mycelium_cli.config import load_config, state_dir
from mycelium_cli.http import CLIError

#: A hook runs while the person works; a server that does not answer in this
#: long is not one to wait for. The report is dropped, the next transition
#: sends the state again.
HOOK_TIMEOUT_S = 4.0

#: At most one reconcile per machine in this window. Every turn of every
#: session ends in a ``Stop``; ten sessions would otherwise each ask the
#: registry (0.5 to 0.7 s) and the server, every turn, to learn the same
#: thing.
RECONCILE_EVERY_S = 60.0


def profile_name() -> str:
    return load_config().current_profile


def logged_in() -> bool:
    try:
        active_profile()
    except CLIError:
        return False
    return True


def open_session(
    host_ref: str, cwd: str, *, title: str | None = None
) -> tuple[dict[str, Any], str]:
    previous = agent_host.load_state(host_ref)
    body = {
        "agent": agent_host.CLAUDE_CODE,
        "host_ref": host_ref,
        "project": agent_host.project_name(cwd) if cwd else None,
        "machine": agent_host.machine_label(),
        "title": title,
        "join_key": previous.join_key if previous else None,
    }
    with client(timeout=HOOK_TIMEOUT_S) as c:
        out = get_json(c.post("/sessions/open", json=body))
    session, key = out["session"], str(out["join_key"])
    agent_host.save_state(
        agent_host.HookState(
            host_ref=host_ref,
            session_id=str(session["id"]),
            sent=_encode(session["attention"], session.get("blocked_on")),
            profile=profile_name(),
            join_key=key,
        )
    )
    return session, key


def _encode(attention: str, blocked_on: str | None) -> str:
    return f"{attention}:{blocked_on or ''}"


def report(
    host_ref: str,
    cwd: str,
    attention: str,
    blocked_on: str | None,
    *,
    title: str | None = None,
) -> None:
    """Send the session's attention, opening the session first if this
    machine never opened it (hooks installed mid-session) or the server
    closed it."""
    state = agent_host.load_state(host_ref)
    if state is None:
        open_session(host_ref, cwd, title=title)
        state = agent_host.load_state(host_ref)
        if state is None:
            raise CLIError(f"the session state for {host_ref} could not be written")
    body = {
        "session_id": state.session_id,
        "attention": attention,
        "blocked_on": blocked_on,
        "title": title,
    }
    with client(timeout=HOOK_TIMEOUT_S) as c:
        resp = c.post("/sessions/report", json=body)
        if resp.status_code == 404 and "session.not_found" in resp.text:
            # Reopened with the key it had, so the agent's stays valid.
            session, _ = open_session(host_ref, cwd, title=title)
            body["session_id"] = session["id"]
            resp = c.post("/sessions/report", json=body)
        get_json(resp)
    state = agent_host.load_state(host_ref) or state
    state.sent = _encode(attention, blocked_on)
    agent_host.save_state(state)


def close(host_ref: str) -> None:
    state = agent_host.load_state(host_ref)
    agent_host.drop_state(host_ref)
    if state is None:
        return
    with client(timeout=HOOK_TIMEOUT_S) as c:
        resp = c.post("/sessions/close", json={"session_id": state.session_id})
        if resp.status_code == 404:
            return  # already closed: what the hook wanted
        get_json(resp)


def _stamp() -> Path:
    return state_dir() / "reconcile.stamp"


def reconcile(*, force: bool = False, live: Iterable[agent_host.LiveSession] | None = None) -> bool:
    """Tell the server which sessions are alive on this machine. Throttled
    to one per :data:`RECONCILE_EVERY_S` unless ``force``. Returns whether it
    ran."""
    stamp = _stamp()
    if not force:
        try:
            if time.time() - stamp.stat().st_mtime < RECONCILE_EVERY_S:
                return False
        except OSError:
            pass
    stamp.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp.touch()
    alive = list(live) if live is not None else agent_host.live_sessions()
    refs = [s.host_ref for s in alive]
    with client(timeout=HOOK_TIMEOUT_S) as c:
        out = get_json(
            c.post(
                "/sessions/reconcile",
                json={
                    "agent": agent_host.CLAUDE_CODE,
                    "machine": agent_host.machine_label(),
                    "alive": refs,
                },
            )
        )
    # Forget only what the server closed. Not "what the registry did not
    # list": a session that has just started may not be listed yet, and the
    # reconcile a SessionStart launches runs exactly then. Dropping its file
    # lost the join key, and the next reopening issued a new one, which left
    # the agent's context stale (measured end to end on 2026-11-26). The
    # server applies the grace that protects a new session; this follows it.
    closed = {str(x) for x in out.get("closed") or []}
    for ref, st in agent_host.all_states().items():
        if st.session_id in closed:
            agent_host.drop_state(ref)
    return True
