"""The relay that writes a person's messages into the Claude Code session
they were sent to (docs/adr/0079).

The server holds the messages and knows no host. This module is the Claude
Code side: a process started by ``SessionStart`` that asks the server every
:data:`POLL_S` seconds for the next message of its session, writes it into
the session's inbox socket, and says what happened. Delivery is at most
once: a message is claimed before it is written and never written again,
because an instruction carried out twice cannot be undone.

One relay per Claude Code process, not per session. ``/clear`` and
``/resume`` give the process a new session id and fire ``SessionStart``
again in it; a relay per session would leave one more behind at each. So the
relay locks a file named after the process and reads, at every poll, which
session the process is in now: the hook writes it there before starting a
relay, and a relay that finds the lock taken exits, leaving the one already
running to read the new session at its next poll.

Everything the relay needs comes from the hook's environment, measured on
Claude Code 2.1.332: ``CLAUDE_CODE_MESSAGING_SOCKET``,
``CLAUDE_CODE_MESSAGING_TOKEN`` and ``CLAUDE_PID``, the process the hook is
a child of. The token stays in this process's memory; it is never written to
a file or sent to the server.

The relay exits at the first poll that finds that process gone. A dead
process's pid is not taken again within one poll: on the machine this was
measured on, pids advanced by about a hundred a second against a
``pid_max`` of 4 194 304, so a pid comes back after hours, not seconds.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from mycelium_cli import agent_host, session_hook
from mycelium_cli.cmds._common import client, get_json
from mycelium_cli.config import state_dir
from mycelium_cli.session_sync import HOOK_TIMEOUT_S

#: How often the relay asks for a message: the longest a message waits once
#: it is written on the page. Ten sessions make two requests a second, each
#: an indexed read (docs/adr/0079, section 3).
POLL_S = 5.0

#: A socket that does not take two short lines in this long is not a session
#: that will.
_WRITE_TIMEOUT_S = 5.0

#: The longest failure the server stores.
_FAILURE_CHARS = 300


@dataclass(frozen=True, slots=True)
class Inbox:
    """The way into one Claude Code process."""

    socket_path: str
    token: str
    host_pid: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Inbox | None:
        """None when the process offers no way in: a host older than the
        inbox, a bare ``-p`` session, which binds no socket."""
        path = env.get("CLAUDE_CODE_MESSAGING_SOCKET") or ""
        token = env.get("CLAUDE_CODE_MESSAGING_TOKEN") or ""
        pid = env.get("CLAUDE_PID") or ""
        if not (path and token and pid.isdigit()):
            return None
        return cls(socket_path=path, token=token, host_pid=int(pid))


def frames(token: str, text: str) -> bytes:
    """What the relay writes: an auth line, then the message as a user turn,
    one JSON object per line. The second line's shape is not on Claude
    Code's public page; the armed end-to-end test is what notices if it
    changes (docs/adr/0079, what this does not cover)."""
    auth = {"type": "auth", "token": token}
    message = {"type": "user", "message": {"role": "user", "content": text}}
    return (json.dumps(auth) + "\n" + json.dumps(message) + "\n").encode()


def write(inbox: Inbox, text: str) -> str | None:
    """Write one message into the session. The failure, or None when the
    socket took it; it answers nothing either way, so None is not proof that
    the session read it."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(_WRITE_TIMEOUT_S)
            s.connect(inbox.socket_path)
            s.sendall(frames(inbox.token, text))
    except OSError as exc:
        return f"the session's inbox refused the message: {type(exc).__name__}: {exc}"[
            :_FAILURE_CHARS
        ]
    return None


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists; it is not ours to signal
    return True


# ---------------------------------------------------------------------------
# Which session the process is in
# ---------------------------------------------------------------------------


def _dir() -> Path:
    return state_dir() / "relay"


def _pointer(host_pid: int) -> Path:
    return _dir() / f"{host_pid}.session"


def _lock(host_pid: int) -> Path:
    return _dir() / f"{host_pid}.lock"


def point(host_pid: int, host_ref: str) -> None:
    """Say which session the process is in now. Atomic: the relay reads it
    at every poll."""
    d = _dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(host_ref)
        os.replace(tmp, _pointer(host_pid))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _current_session(host_pid: int) -> str | None:
    """The Mycelium session id of the session the process is in, or None
    when its hooks have not opened one."""
    try:
        ref = _pointer(host_pid).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    state = agent_host.load_state(ref) if ref else None
    return state.session_id if state else None


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class Relay:
    """One poll at a time, so that a test drives it without a clock."""

    def __init__(self, inbox: Inbox, client: httpx.Client) -> None:
        self.inbox = inbox
        self.client = client
        #: An outcome the server did not take (it was unreachable). Sent
        #: again before anything else is claimed: without it, a message the
        #: session received would read as "may not have arrived".
        self.unsent: dict[str, Any] | None = None

    def poll(self) -> None:
        if self.unsent is not None:
            self._settle(self.unsent)
        session_id = _current_session(self.inbox.host_pid)
        if session_id is None:
            return
        resp = self.client.post("/sessions/messages/claim", json={"session_id": session_id})
        if resp.status_code == 404:
            # Closed by the server while the process lives (three silent
            # days); the session's next hook reopens it. Not a reason to exit.
            return
        message = get_json(resp)["message"]
        if message is None:
            return
        failure = write(self.inbox, str(message["text"]))
        self._settle(
            {"message_id": message["id"], "delivered": failure is None, "failure": failure}
        )

    def _settle(self, outcome: dict[str, Any]) -> None:
        self.unsent = outcome
        get_json(self.client.post("/sessions/messages/outcome", json=outcome))
        self.unsent = None


def run(inbox: Inbox) -> None:
    """Serve the process until it is gone. Returns at once when another
    relay already serves it."""
    _dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = _lock(inbox.host_pid)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        last_error = ""
        try:
            with client(timeout=HOOK_TIMEOUT_S) as c:
                relay = Relay(inbox, c)
                while alive(inbox.host_pid):
                    try:
                        relay.poll()
                        last_error = ""
                    except Exception as exc:  # a server that is down is waited out, not fatal
                        error = f"relay: {type(exc).__name__}: {exc}"
                        if error != last_error:  # one line per outage, not one per poll
                            session_hook.log(error)
                        last_error = error
                    time.sleep(POLL_S)
        finally:
            for p in (_pointer(inbox.host_pid), lock_path):
                with contextlib.suppress(OSError):
                    p.unlink()
