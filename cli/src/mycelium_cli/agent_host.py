"""What this machine knows about the agent sessions running on it
(docs/adr/0078): which are alive and what each is doing, which terminal pane
each is in, what project and machine to call it, and which Mycelium session
each one opened.

Standard library only, on purpose. The hook entry point imports this module
on every tool call of every session (``session_hook``), and the CLI with its
HTTP client takes 0.7 to 0.9 s to start where a bare interpreter takes 0.1 s
(measured 2026-11-26); a hook that cost a second of CPU per tool call across
ten sessions would be a tax nobody agreed to.

The live registry is Claude Code's own (``claude agents --json``), not a
reading of terminal text: it is structured, documented, and it is the only
source that knows a session is at a permission prompt right now. It is a
research preview, so its shape is checked and an unknown one is an error
naming the command, never a silent empty list.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from mycelium_cli.config import state_dir

#: The host this module knows. The server's ``agent`` value for it.
CLAUDE_CODE = "claude_code"

LiveStatus = Literal["busy", "waiting", "idle"]
_LIVE_STATUSES: frozenset[str] = frozenset({"busy", "waiting", "idle"})

#: ``claude agents --json`` measured at 0.5 to 0.7 s; ten is a bound for a
#: loaded machine, not an expectation.
_REGISTRY_TIMEOUT_S = 10.0


class HostRegistryError(Exception):
    """The host's registry could not be read, or answered a shape this
    module does not know. Carries the command, so the message says what to
    run to see for oneself."""

    def __init__(self, message: str, command: str) -> None:
        super().__init__(f"{message} (from `{command}`)")
        self.command = command


@dataclass(frozen=True, slots=True)
class LiveSession:
    """One session the host lists as alive on this machine."""

    host_ref: str
    pid: int | None
    cwd: str
    kind: str
    status: LiveStatus | None
    waiting_for: str | None
    name: str | None


@dataclass(frozen=True, slots=True)
class Pane:
    """Where a session is in tmux: what ``switch-client`` and a person need."""

    pane_id: str
    session: str
    window_index: str
    window_name: str

    @property
    def target(self) -> str:
        return f"{self.session}:{self.window_index}"


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def machine_label() -> str:
    """``MYCELIUM_MACHINE_LABEL``, else the short hostname. Short because the
    domain part is the same on every machine a person has and is noise on a
    row."""
    label = (os.environ.get("MYCELIUM_MACHINE_LABEL") or "").strip()
    return label or socket.gethostname().split(".")[0]


def _git(cwd: str, *args: str) -> str | None:
    try:
        r = subprocess.run(  # noqa: S603  # argv list, no shell; the directory is the value of -C, never a command or an option
            ["git", "-C", cwd, *args],  # noqa: S607  # found on PATH on purpose: claude, git, ps and tmux are where the person installed them
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def project_name(cwd: str) -> str:
    """What the session works on, as a person names it: the repository.

    A linked worktree is named after its repository and the worktree, so two
    sessions on two worktrees of one project read apart (``mycelium ·
    sessions``); a directory outside git is its own name. Never a path: the
    server's copy is read by every member of the workspace."""
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if top is None:
        return Path(cwd).name or cwd
    common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    repo = Path(common).parent.name if common else Path(top).name
    here = Path(top).name
    return repo if here == repo else f"{repo} · {here}"


# ---------------------------------------------------------------------------
# The host's registry
# ---------------------------------------------------------------------------

_REGISTRY_COMMAND = ("claude", "agents", "--json")


def _parse_registry(raw: str) -> list[LiveSession]:
    cmd = " ".join(_REGISTRY_COMMAND)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HostRegistryError("the session registry did not answer JSON", cmd) from exc
    if not isinstance(data, list):
        raise HostRegistryError("the session registry did not answer a list", cmd)
    out: list[LiveSession] = []
    for row in data:
        if not isinstance(row, dict) or "cwd" not in row or "kind" not in row:
            raise HostRegistryError("a registry row has no cwd or kind", cmd)
        status = row.get("status")
        if status is not None and status not in _LIVE_STATUSES:
            # A new status is a new meaning; showing it as one of ours would
            # be a guess about whether the person is needed.
            raise HostRegistryError(f"unknown session status {status!r}", cmd)
        ref = row.get("sessionId")
        if not ref:
            continue  # a dispatched session that has not started yet
        pid = row.get("pid")
        out.append(
            LiveSession(
                host_ref=str(ref),
                pid=int(pid) if isinstance(pid, int) else None,
                cwd=str(row["cwd"]),
                kind=str(row["kind"]),
                status=status,
                waiting_for=(str(row["waitingFor"]) if row.get("waitingFor") else None),
                name=(str(row["name"]) if row.get("name") else None),
            )
        )
    return out


def live_sessions() -> list[LiveSession]:
    """Every session Claude Code lists as alive on this machine."""
    cmd = " ".join(_REGISTRY_COMMAND)
    try:
        r = subprocess.run(  # noqa: S603  # argv is a literal list, no shell, no input from outside
            list(_REGISTRY_COMMAND),
            capture_output=True,
            text=True,
            timeout=_REGISTRY_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:
        raise HostRegistryError("Claude Code is not installed here", cmd) from exc
    except subprocess.TimeoutExpired as exc:
        raise HostRegistryError(f"no answer in {_REGISTRY_TIMEOUT_S:.0f} s", cmd) from exc
    if r.returncode != 0:
        raise HostRegistryError(f"exited {r.returncode}: {r.stderr.strip()[:200]}", cmd)
    return _parse_registry(r.stdout)


def attention_of(live: LiveSession) -> tuple[str, str | None]:
    """The server's attention for what the host says right now.

    ``waiting`` on a permission prompt or a sandbox or worker request is
    blocked on a permission; any other wait (``input needed``, a dialog) is
    blocked on a question. ``idle`` is the person's turn."""
    if live.status == "waiting":
        w = (live.waiting_for or "").lower()
        if "permission" in w or "request" in w:
            return "blocked", "permission"
        return "blocked", "question"
    if live.status == "idle":
        return "your_turn", None
    return "working", None


# ---------------------------------------------------------------------------
# tmux
# ---------------------------------------------------------------------------


def _parents() -> dict[int, int]:
    """pid -> parent pid for every process, from ``ps``, which answers the
    same columns on Linux and macOS (``/proc`` exists on one of them)."""
    try:
        r = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid="],  # noqa: S607  # found on PATH on purpose: claude, git, ps and tmux are where the person installed them
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out: dict[int, int] = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            out[int(parts[0])] = int(parts[1])
    return out


def tmux_panes() -> dict[int, Pane]:
    """Every tmux pane by the pid of the process it runs. Empty when tmux is
    not running: a session outside tmux is still listed, without a place to
    jump to."""
    fmt = "#{pane_pid}\t#{pane_id}\t#{session_name}\t#{window_index}\t#{window_name}"
    try:
        r = subprocess.run(  # noqa: S603  # argv is a literal list, no shell, no input from outside
            ["tmux", "list-panes", "-a", "-F", fmt],  # noqa: S607  # found on PATH on purpose: claude, git, ps and tmux are where the person installed them
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if r.returncode != 0:
        return {}
    panes: dict[int, Pane] = {}
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 5 and parts[0].isdigit():
            panes[int(parts[0])] = Pane(
                pane_id=parts[1], session=parts[2], window_index=parts[3], window_name=parts[4]
            )
    return panes


def panes_of(sessions: list[LiveSession]) -> dict[str, Pane]:
    """host_ref -> the pane its process runs in, by walking each pid up to a
    pane's pid. Measured on 2026-11-26: 10 interactive sessions out of 10."""
    panes = tmux_panes()
    if not panes:
        return {}
    parents = _parents()
    out: dict[str, Pane] = {}
    for s in sessions:
        pid, seen = s.pid, 0
        while pid and pid > 1 and seen < 64:
            if pid in panes:
                out[s.host_ref] = panes[pid]
                break
            pid = parents.get(pid, 0)
            seen += 1
    return out


def switch_to(pane: Pane) -> None:
    """Bring the person to the pane: its session, its window, the pane."""
    subprocess.run(  # noqa: S603  # argv is a literal list, no shell, no input from outside
        [  # noqa: S607  # found on PATH on purpose: claude, git, ps and tmux are where the person installed them
            "tmux",
            "switch-client",
            "-t",
            pane.session,
            ";",
            "select-window",
            "-t",
            pane.target,
            ";",
            "select-pane",
            "-t",
            pane.pane_id,
        ],
        check=False,
    )


# ---------------------------------------------------------------------------
# What the hooks remember
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HookState:
    """What the hooks of one host session wrote down: the Mycelium session it
    opened, the last attention they sent, the profile they sent it on, and
    the join key the agent was given.

    ``sent`` is what makes a ``PostToolUse`` cost a file read and nothing
    else when the session was already reported as working. ``join_key`` is
    presented again whenever the session is reopened, so the server keeps it
    and the key in the agent's context stays valid. The directory is the
    user's own (mode 0700); the key grants membership of a worker in this
    session's row and nothing else."""

    host_ref: str
    session_id: str
    sent: str
    profile: str | None = None
    join_key: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "host_ref": self.host_ref,
            "session_id": self.session_id,
            "sent": self.sent,
            "profile": self.profile,
            "join_key": self.join_key,
        }


def sessions_dir() -> Path:
    return state_dir() / "sessions"


def _state_path(host_ref: str) -> Path:
    # The host's id is a uuid today; anything else is reduced to a safe name
    # rather than trusted as a path component.
    safe = "".join(c for c in host_ref if c.isalnum() or c in "-_")[:128] or "unnamed"
    return sessions_dir() / f"{safe}.json"


def load_state(host_ref: str) -> HookState | None:
    try:
        data = json.loads(_state_path(host_ref).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return HookState(
            host_ref=str(data["host_ref"]),
            session_id=str(data["session_id"]),
            sent=str(data.get("sent") or ""),
            profile=data.get("profile"),
            join_key=data.get("join_key"),
        )
    except KeyError:
        return None


def save_state(state: HookState) -> None:
    """Atomically: two hooks of one session can run at once (async hooks are
    not serialised), and a half-written file would lose the session id."""
    d = sessions_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state.to_json(), f)
        os.replace(tmp, _state_path(state.host_ref))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def drop_state(host_ref: str) -> None:
    _state_path(host_ref).unlink(missing_ok=True)


def all_states() -> dict[str, HookState]:
    out: dict[str, HookState] = {}
    d = sessions_dir()
    if not d.is_dir():
        return out
    for p in d.glob("*.json"):
        st = load_state(p.stem)
        if st is not None:
            out[st.host_ref] = st
    return out


def log_path() -> Path:
    return state_dir() / "hooks.log"
