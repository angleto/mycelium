"""The relay that writes messages from the sessions page into a Claude Code
session (docs/adr/0079, phase B).

What is pinned here: the two lines written to the inbox; a refused write
reported as a failure, never as delivered; one claim per poll, settled
before the next; an outcome the server missed sent again before anything
else is claimed; the session a process is in read at every poll, so
``/clear`` needs no second relay; one relay per process; the relay ending
with its process; the hook opening a session as accepting messages only when
the machine opted in and the process has an inbox; a reopening that keeps
it; the settings switch. The server side is tested against Postgres in
core/tests/test_session_messages.py. The end-to-end test against a real
Claude Code session is at the bottom and runs only when armed.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from mycelium_cli import agent_host, session_hook, session_relay
from mycelium_cli import session_sync as sync
from mycelium_cli.cmds import session as session_cmd

TOKEN = "tok-123"  # a test value, written to a socket the test owns


@pytest.fixture(autouse=True)
def _state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MYCELIUM_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A Unix socket path is limited to about a hundred bytes, which a
    pytest tmp_path can exceed."""
    d = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / f"mrelay-{os.getpid()}"  # noqa: S108  # short on purpose, removed below
    d.mkdir(mode=0o700, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


class InboxServer:
    """A Unix socket that records what each connection wrote."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.received: list[bytes] = []
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(path))
        self._sock.listen()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                chunks = []
                while chunk := conn.recv(65536):
                    chunks.append(chunk)
                self.received.append(b"".join(chunks))

    def wait_for(self, n: int) -> list[bytes]:
        deadline = time.monotonic() + 5
        while len(self.received) < n and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.received

    def close(self) -> None:
        self._sock.close()


@pytest.fixture
def inbox_server(short_dir: Path) -> Iterator[InboxServer]:
    server = InboxServer(short_dir / "in.sock")
    yield server
    server.close()


def _inbox(path: Path, pid: int | None = None) -> session_relay.Inbox:
    return session_relay.Inbox(socket_path=str(path), token=TOKEN, host_pid=pid or os.getpid())


# ---------------------------------------------------------------------------
# What is written
# ---------------------------------------------------------------------------


def test_the_relay_writes_an_auth_line_and_one_user_turn() -> None:
    assert session_relay.frames(TOKEN, "rebase onto 2.4\nthen stop") == (
        b'{"type": "auth", "token": "tok-123"}\n'
        b'{"type": "user", "message": {"role": "user", "content": "rebase onto 2.4\\nthen stop"}}\n'
    )


def test_a_write_reaches_the_socket_whole(inbox_server: InboxServer) -> None:
    assert session_relay.write(_inbox(inbox_server.path), "hello") is None
    [got] = inbox_server.wait_for(1)
    assert got == session_relay.frames(TOKEN, "hello")


def test_a_refused_write_is_a_failure_that_names_why(short_dir: Path) -> None:
    gone = short_dir / "nobody.sock"
    failure = session_relay.write(_inbox(gone), "hello")
    assert failure is not None
    assert "FileNotFoundError" in failure
    assert len(failure) <= 300


def test_a_process_without_an_inbox_has_none() -> None:
    full = {
        "CLAUDE_CODE_MESSAGING_SOCKET": "/run/user/1/cc-socks/7.sock",
        "CLAUDE_CODE_MESSAGING_TOKEN": TOKEN,
        "CLAUDE_PID": "7",
    }
    assert session_relay.Inbox.from_env(full) == session_relay.Inbox(
        "/run/user/1/cc-socks/7.sock", TOKEN, 7
    )
    for missing in full:
        assert session_relay.Inbox.from_env({k: v for k, v in full.items() if k != missing}) is None
    assert session_relay.Inbox.from_env({**full, "CLAUDE_PID": "x"}) is None


# ---------------------------------------------------------------------------
# One poll
# ---------------------------------------------------------------------------


Handler = Callable[[httpx.Request], httpx.Response]


class Server:
    """The two relay operations, scripted."""

    def __init__(self) -> None:
        self.queued: dict[str, list[dict[str, str]]] = {}
        self.claims: list[str] = []
        self.outcomes: list[dict[str, Any]] = []
        self.down = False
        self.outcomes_down = False
        self.closed: set[str] = set()

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503, json={"detail": "down"})
        body = json.loads(request.content)
        if request.url.path == "/sessions/messages/claim":
            sid = body["session_id"]
            self.claims.append(sid)
            if sid in self.closed:
                return httpx.Response(404, json={"detail": {"code": "session.not_found"}})
            queue = self.queued.get(sid) or []
            return httpx.Response(200, json={"message": queue.pop(0) if queue else None})
        assert request.url.path == "/sessions/messages/outcome"
        if self.outcomes_down:
            return httpx.Response(503, json={"detail": "down"})
        self.outcomes.append(body)
        return httpx.Response(200, json={})


@pytest.fixture
def server() -> Server:
    return Server()


@pytest.fixture
def relay(server: Server, inbox_server: InboxServer) -> Iterator[session_relay.Relay]:
    agent_host.save_state(agent_host.HookState("host-a", "sid-a", "working:"))
    inbox = _inbox(inbox_server.path)
    session_relay.point(inbox.host_pid, "host-a")
    with httpx.Client(transport=httpx.MockTransport(server.handle), base_url="http://t") as c:
        yield session_relay.Relay(inbox, c)


def test_a_claimed_message_is_written_and_reported_delivered(
    relay: session_relay.Relay, server: Server, inbox_server: InboxServer
) -> None:
    server.queued["sid-a"] = [{"id": "m-1", "text": "Message from Angelo...\n\nstop"}]

    relay.poll()

    assert inbox_server.wait_for(1) == [
        session_relay.frames(TOKEN, "Message from Angelo...\n\nstop")
    ]
    assert server.outcomes == [{"message_id": "m-1", "delivered": True, "failure": None}]


def test_nothing_queued_writes_nothing(
    relay: session_relay.Relay, server: Server, inbox_server: InboxServer
) -> None:
    relay.poll()
    assert server.claims == ["sid-a"]
    assert server.outcomes == []
    assert inbox_server.received == []


def test_a_session_whose_inbox_is_gone_reports_the_failure(
    relay: session_relay.Relay, server: Server, inbox_server: InboxServer
) -> None:
    inbox_server.close()
    inbox_server.path.unlink()
    server.queued["sid-a"] = [{"id": "m-1", "text": "stop"}]

    relay.poll()

    [outcome] = server.outcomes
    assert outcome["delivered"] is False
    assert "the session's inbox refused the message" in outcome["failure"]


def test_an_outcome_the_server_missed_is_sent_before_the_next_claim(
    relay: session_relay.Relay, server: Server
) -> None:
    """Otherwise a message the session received reads as "may not have
    arrived" after two minutes."""
    server.queued["sid-a"] = [{"id": "m-1", "text": "one"}, {"id": "m-2", "text": "two"}]
    server.outcomes_down = True
    with pytest.raises(Exception):  # noqa: B017  # the CLI error for a 503; its wording is not what is tested
        relay.poll()
    server.outcomes_down, server.down = False, True
    with pytest.raises(Exception):  # noqa: B017  # as above
        relay.poll()
    assert server.claims == ["sid-a"]  # nothing claimed while the outcome is owed

    server.down = False
    relay.poll()

    assert [o["message_id"] for o in server.outcomes] == ["m-1", "m-2"]
    assert server.claims == ["sid-a", "sid-a"]


def test_a_session_the_server_closed_is_waited_on_not_left(
    relay: session_relay.Relay, server: Server
) -> None:
    server.closed.add("sid-a")
    relay.poll()
    relay.poll()
    assert server.claims == ["sid-a", "sid-a"]


def test_after_a_clear_the_same_relay_serves_the_new_session(
    relay: session_relay.Relay, server: Server, inbox_server: InboxServer
) -> None:
    """``/clear`` ends one session and starts another in the same process:
    its SessionStart moves the pointer and the relay follows it."""
    relay.poll()
    agent_host.drop_state("host-a")  # SessionEnd of the cleared session
    relay.poll()
    assert server.claims == ["sid-a"]  # between the two sessions: nothing to serve

    agent_host.save_state(agent_host.HookState("host-b", "sid-b", "working:"))
    session_relay.point(relay.inbox.host_pid, "host-b")
    server.queued["sid-b"] = [{"id": "m-9", "text": "after the clear"}]
    relay.poll()

    assert server.claims == ["sid-a", "sid-b"]
    assert inbox_server.wait_for(1) == [session_relay.frames(TOKEN, "after the clear")]


# ---------------------------------------------------------------------------
# The process
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _idle_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"message": None})

    @contextlib.contextmanager
    def stub(**_k: Any) -> Iterator[httpx.Client]:
        with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://t") as c:
            yield c

    monkeypatch.setattr(session_relay, "client", stub)
    monkeypatch.setattr(session_relay, "POLL_S", 0.01)
    yield seen


def test_the_relay_ends_with_its_process_and_leaves_nothing(
    monkeypatch: pytest.MonkeyPatch, _state: Path, short_dir: Path
) -> None:
    host = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.3)"]
    )  # this interpreter, a literal program
    agent_host.save_state(agent_host.HookState("host-a", "sid-a", "working:"))
    session_relay.point(host.pid, "host-a")
    with _idle_server(monkeypatch) as seen:
        done = threading.Thread(
            target=session_relay.run, args=(_inbox(short_dir / "x", host.pid),), daemon=True
        )
        done.start()
        host.wait()
        done.join(timeout=5)

    assert not done.is_alive()
    assert seen  # it served while the process lived
    assert list((_state / "relay").iterdir()) == []


def test_a_second_relay_for_the_same_process_exits_at_once(
    monkeypatch: pytest.MonkeyPatch, short_dir: Path
) -> None:
    """SessionStart fires again on resume, /clear and compaction, and each
    one starts a relay."""
    stop = threading.Event()
    host_pid = os.getpid()
    real_alive = session_relay.alive
    monkeypatch.setattr(session_relay, "alive", lambda pid: not stop.is_set() and real_alive(pid))
    with _idle_server(monkeypatch):
        first = threading.Thread(
            target=session_relay.run, args=(_inbox(short_dir / "x", host_pid),), daemon=True
        )
        first.start()
        time.sleep(0.1)
        second = threading.Thread(
            target=session_relay.run, args=(_inbox(short_dir / "x", host_pid),), daemon=True
        )
        second.start()
        second.join(timeout=0.5)
        second_exited = not second.is_alive()
        stop.set()
        first.join(timeout=5)
        second.join(timeout=5)

    assert second_exited
    assert not first.is_alive()


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------


def _start_hook(
    monkeypatch: pytest.MonkeyPatch, args: list[str], env: dict[str, str]
) -> tuple[list[bool | None], list[str]]:
    for k in ("CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    opened: list[bool | None] = []
    started: list[str] = []

    def fake_open(ref: str, cwd: str, **k: Any) -> tuple[dict[str, Any], str]:
        opened.append(k.get("accepts_messages"))
        return {"id": "sid", "project": "p"}, "key"

    monkeypatch.setattr(sync, "logged_in", lambda: True)
    monkeypatch.setattr(sync, "open_session", fake_open)
    monkeypatch.setattr(session_hook, "_detached", started.append)
    payload = {"hook_event_name": "SessionStart", "session_id": "host-a", "cwd": "/w/p"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr("sys.stdout", io.StringIO())
    assert session_hook.main(args) == 0
    return opened, started


_INBOX_ENV = {
    "CLAUDE_CODE_MESSAGING_SOCKET": "/run/user/1/cc-socks/4242.sock",
    "CLAUDE_CODE_MESSAGING_TOKEN": TOKEN,
    "CLAUDE_PID": "4242",
}


def test_an_opted_in_machine_opens_the_session_accepting_messages_and_starts_its_relay(
    monkeypatch: pytest.MonkeyPatch, _state: Path
) -> None:
    opened, started = _start_hook(monkeypatch, ["--with-relay"], _INBOX_ENV)
    assert opened == [True]
    assert started == ["--relay", "--reconcile"]
    assert (_state / "relay" / "4242.session").read_text(encoding="utf-8") == "host-a"


@pytest.mark.parametrize(
    ("args", "env"),
    [([], _INBOX_ENV), (["--with-relay"], {})],
    ids=["not opted in", "no inbox in the process"],
)
def test_otherwise_the_session_accepts_no_messages_and_no_relay_starts(
    monkeypatch: pytest.MonkeyPatch, args: list[str], env: dict[str, str]
) -> None:
    opened, started = _start_hook(monkeypatch, args, env)
    assert opened == [False]
    assert started == ["--reconcile"]


def test_a_reopening_by_another_hook_keeps_what_session_start_decided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """report() reopens a session the server closed; sessions.open sets
    accepts_messages on every open, so a reopening that did not repeat it
    would turn the composer off for a session whose relay still runs."""
    agent_host.save_state(
        agent_host.HookState("host-a", "sid", "working:", join_key="k", accepts_messages=True)
    )
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "session": {"id": "sid", "attention": "working", "blocked_on": None},
                "join_key": "k",
            },
        )

    @contextlib.contextmanager
    def stub(**_k: Any) -> Iterator[httpx.Client]:
        with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://t") as c:
            yield c

    monkeypatch.setattr(sync, "client", stub)
    monkeypatch.setattr(sync, "profile_name", lambda: "default")

    sync.open_session("host-a", "/w/p")

    assert bodies[0]["accepts_messages"] is True
    state = agent_host.load_state("host-a")
    assert state is not None and state.accepts_messages is True


# ---------------------------------------------------------------------------
# install-hooks
# ---------------------------------------------------------------------------


def _start_command(settings: dict[str, Any]) -> str:
    [h] = [
        h
        for g in settings["hooks"]["SessionStart"]
        for h in g["hooks"]
        if "mycelium-hook" in h["command"]
    ]
    return str(h["command"])


def test_the_relay_is_switched_on_and_off_on_session_start_only() -> None:
    cmd = "/usr/local/bin/mycelium-hook"
    settings: dict[str, Any] = {}

    session_cmd.merge_hooks(settings, cmd)
    assert _start_command(settings) == cmd

    assert session_cmd.merge_hooks(settings, cmd, relay=True) == ["SessionStart"]
    assert _start_command(settings) == f"{cmd} --with-relay"
    assert settings["hooks"]["Stop"][0]["hooks"][0]["command"] == cmd

    assert session_cmd.merge_hooks(settings, cmd) == []  # rerun without a switch: unchanged
    assert session_cmd.merge_hooks(settings, cmd, relay=True) == []
    assert _start_command(settings) == f"{cmd} --with-relay"

    assert session_cmd.merge_hooks(settings, cmd, relay=False) == ["SessionStart"]
    assert _start_command(settings) == cmd


# ---------------------------------------------------------------------------
# Against a real Claude Code session
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("MYCELIUM_E2E_CLAUDE") != "1",
    reason="starts a real Claude Code session in tmux; set MYCELIUM_E2E_CLAUDE=1",
)
def test_a_real_session_receives_what_the_relay_writes(tmp_path: Path) -> None:
    """The one test of the frame format, which is not on Claude Code's
    public page. A SessionStart hook starts, detached, a process that writes
    one message with this module, as the relay does; the session's
    transcript must then hold it as a user turn. The message asks for a
    one-word answer, so the turn it starts is short."""
    if shutil.which("claude") is None or shutil.which("tmux") is None:
        pytest.skip("needs claude and tmux on PATH")
    marker = f"relay-e2e-{os.getpid()}-{int(time.time())}"
    text = f"{marker}: this is an automated test of message delivery. Reply with the word OK only."
    payload_file = tmp_path / "payload.json"
    sender = tmp_path / "send.py"
    sender.write_text(
        "import os, sys, time\n"
        "from mycelium_cli import session_relay\n"
        "time.sleep(4)\n"
        "inbox = session_relay.Inbox.from_env(os.environ)\n"
        f"print(session_relay.write(inbox, {text!r}) if inbox else 'no inbox', file=sys.stderr)\n",
        encoding="utf-8",
    )
    hook = tmp_path / "hook.py"
    hook.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        f"open({str(payload_file)!r}, 'w').write(sys.stdin.read())\n"
        f"err = open({str(tmp_path / 'send.err')!r}, 'w')\n"
        f"subprocess.Popen([sys.executable, {str(sender)!r}], stdin=subprocess.DEVNULL,"
        " stdout=subprocess.DEVNULL, stderr=err, start_new_session=True)\n",
        encoding="utf-8",
    )
    hook.chmod(0o700)
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": str(hook)}]}]}}
        ),
        encoding="utf-8",
    )
    name = f"mrelay-e2e-{os.getpid()}"
    cwd = os.environ.get("MYCELIUM_E2E_CLAUDE_CWD") or os.getcwd()  # a directory Claude Code trusts
    subprocess.run(  # noqa: S603  # tmux and claude with literal arguments
        ["tmux", "new-session", "-d", "-s", name, "-c", cwd, f"claude --settings {settings}"],  # noqa: S607  # found on PATH on purpose
        check=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not payload_file.exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        transcript = Path(json.loads(payload_file.read_text(encoding="utf-8"))["transcript_path"])
        deadline = time.monotonic() + 60
        found = False
        while not found and time.monotonic() < deadline:
            time.sleep(1)
            if transcript.exists():
                for line in transcript.read_text(encoding="utf-8").splitlines():
                    row = json.loads(line)
                    if row.get("type") == "user" and marker in json.dumps(row.get("message")):
                        found = True
                        break
        err_file = tmp_path / "send.err"
        err = err_file.read_text(encoding="utf-8") if err_file.exists() else ""
        assert found, f"the session never received the message; the sender said: {err!r}"
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False)  # noqa: S603, S607  # literal arguments
