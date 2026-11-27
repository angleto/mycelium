"""The CLI side of the sessions a person steers (docs/adr/0078).

What the CLI owns, and what these tests pin: reading the host's registry and
refusing a shape it does not know; naming a project the way a person does;
the hook saying nothing when nothing changed, never failing the session, and
handing the agent its key; the settings merge leaving other hooks alone; the
words a row is drawn with; a stale key not costing the shell its worker.
The server's behaviour is tested against Postgres in core/tests.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from mycelium_cli import agent_host, session_hook
from mycelium_cli import session_sync as sync
from mycelium_cli.cmds import session as session_cmd
from mycelium_cli.cmds import worker as worker_cmd
from mycelium_cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MYCELIUM_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def _registry(*rows: dict[str, Any]) -> str:
    return json.dumps(list(rows))


def test_the_registry_is_read_with_its_waiting_reason() -> None:
    rows = agent_host._parse_registry(
        _registry(
            {
                "pid": 10,
                "cwd": "/w/humus",
                "kind": "interactive",
                "startedAt": 1,
                "sessionId": "s-1",
                "status": "waiting",
                "waitingFor": "permission prompt",
                "name": "humus-31",
            },
            # Not started yet: nothing to show, and nothing to key it by.
            {"cwd": "/w/x", "kind": "background", "startedAt": 2},
        )
    )
    assert [(r.host_ref, r.status, r.waiting_for, r.name) for r in rows] == [
        ("s-1", "waiting", "permission prompt", "humus-31")
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps({"sessions": []}),
        _registry({"kind": "interactive"}),
        _registry({"cwd": "/w", "kind": "interactive", "sessionId": "s", "status": "sleeping"}),
    ],
)
def test_a_registry_shape_this_cli_does_not_know_is_an_error_naming_the_command(
    raw: str,
) -> None:
    with pytest.raises(agent_host.HostRegistryError) as exc:
        agent_host._parse_registry(raw)
    assert "claude agents --json" in str(exc.value)


@pytest.mark.parametrize(
    ("status", "waiting_for", "expected"),
    [
        ("waiting", "permission prompt", ("blocked", "permission")),
        ("waiting", "sandbox request", ("blocked", "permission")),
        ("waiting", "input needed", ("blocked", "question")),
        ("waiting", "dialog open", ("blocked", "question")),
        ("idle", None, ("your_turn", None)),
        ("busy", None, ("working", None)),
    ],
)
def test_the_hosts_status_becomes_the_servers_attention(
    status: str, waiting_for: str | None, expected: tuple[str, str | None]
) -> None:
    live = agent_host.LiveSession("s", 1, "/w", "interactive", status, waiting_for, None)  # type: ignore[arg-type]  # parametrized literal
    assert agent_host.attention_of(live) == expected


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)  # noqa: S603, S607  # a literal git argv against a temporary repository


def test_a_project_is_named_after_its_repository_and_a_worktree_after_both(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "mycelium"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git(
        "-c",
        "user.email=t@e",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
        cwd=repo,
    )
    _git("worktree", "add", "-q", str(tmp_path / "wt" / "sessions"), cwd=repo)
    (repo / "sub").mkdir()

    assert agent_host.project_name(str(repo / "sub")) == "mycelium"
    assert agent_host.project_name(str(tmp_path / "wt" / "sessions")) == "mycelium · sessions"
    plain = tmp_path / "notes"
    plain.mkdir()
    assert agent_host.project_name(str(plain)) == "notes"


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------


def _run_hook(payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    code = session_hook.main([])
    return code, buf.getvalue()


def _boom(*_a: Any, **_k: Any) -> None:
    raise AssertionError("the hook reached the server")


def test_a_tool_call_in_a_session_already_working_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_host.save_state(agent_host.HookState("s-1", "sid", "working:"))
    monkeypatch.setattr(sync, "report", _boom)
    monkeypatch.setattr(sync, "logged_in", lambda: True)

    code, _ = _run_hook({"hook_event_name": "PostToolUse", "session_id": "s-1"}, monkeypatch)

    assert code == 0


@pytest.mark.parametrize(
    ("event", "extra", "expected"),
    [
        ("UserPromptSubmit", {}, ("working", None)),
        ("PostToolUse", {}, ("working", None)),
        ("Notification", {"notification_type": "permission_prompt"}, ("blocked", "permission")),
        ("Notification", {"notification_type": "elicitation_dialog"}, ("blocked", "question")),
        ("Stop", {}, ("your_turn", None)),
    ],
)
def test_each_event_sends_what_it_means(
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    extra: dict[str, Any],
    expected: tuple[str, str | None],
) -> None:
    sent: list[tuple[str, str | None]] = []
    monkeypatch.setattr(sync, "logged_in", lambda: True)
    monkeypatch.setattr(sync, "report", lambda _r, _c, a, b, **_k: sent.append((a, b)))
    monkeypatch.setattr(sync, "reconcile", lambda **_k: False)

    _run_hook({"hook_event_name": event, "session_id": "s-2", **extra}, monkeypatch)

    assert sent == [expected]


@pytest.mark.parametrize("kind", ["idle_prompt", "auth_success", "quota_auto_resume_fired"])
def test_a_notification_that_is_not_a_prompt_says_nothing(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(sync, "report", _boom)
    _run_hook(
        {"hook_event_name": "Notification", "session_id": "s", "notification_type": kind},
        monkeypatch,
    )


def test_a_failure_is_logged_and_never_fails_the_session(
    monkeypatch: pytest.MonkeyPatch, _state: Path
) -> None:
    def down(*_a: Any, **_k: Any) -> None:
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(sync, "logged_in", lambda: True)
    monkeypatch.setattr(sync, "report", down)

    code, out = _run_hook({"hook_event_name": "Stop", "session_id": "s-3"}, monkeypatch)

    assert code == 0
    assert out == ""
    assert "unreachable" in (_state / "hooks.log").read_text(encoding="utf-8")


def test_session_start_hands_the_agent_its_key_and_the_shell_its_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / "env"
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
    monkeypatch.setattr(sync, "logged_in", lambda: True)
    monkeypatch.setattr(
        sync,
        "open_session",
        lambda ref, cwd, **_k: ({"id": "sid", "project": "humus"}, "k3y-value"),
    )
    monkeypatch.setattr(session_hook, "_detached", lambda _mode: None)

    code, out = _run_hook(
        {"hook_event_name": "SessionStart", "session_id": "s-4", "cwd": "/w/humus"}, monkeypatch
    )

    assert code == 0
    ctx = json.loads(out)["hookSpecificOutput"]
    assert ctx["hookEventName"] == "SessionStart"
    assert "join_key='k3y-value'" in ctx["additionalContext"]
    assert "humus" in ctx["additionalContext"]
    assert env_file.read_text(encoding="utf-8") == "export MYCELIUM_SESSION_KEY=k3y-value\n"


def test_without_a_login_the_hook_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "logged_in", lambda: False)
    monkeypatch.setattr(sync, "open_session", _boom)
    code, out = _run_hook({"hook_event_name": "SessionStart", "session_id": "s"}, monkeypatch)
    assert (code, out) == (0, "")


# ---------------------------------------------------------------------------
# install-hooks
# ---------------------------------------------------------------------------


def test_the_merge_adds_the_hook_once_and_leaves_other_hooks_alone() -> None:
    theirs = {"type": "command", "command": "python3 ~/.claude/handbook/gate.py", "timeout": 5}
    settings: dict[str, Any] = {
        "model": "x",
        "hooks": {"SessionStart": [{"hooks": [theirs]}]},
    }

    added = session_cmd.merge_hooks(settings, "/usr/local/bin/mycelium-hook")
    again = session_cmd.merge_hooks(settings, "/usr/local/bin/mycelium-hook")

    assert set(added) == {e for e, _, _ in session_cmd._EVENTS}
    assert again == []
    assert settings["model"] == "x"
    assert settings["hooks"]["SessionStart"][0]["hooks"] == [theirs]
    start = settings["hooks"]["SessionStart"][1]["hooks"][0]
    assert "async" not in start  # it injects the key, which an async hook cannot
    assert settings["hooks"]["PostToolUse"][0]["hooks"][0]["async"] is True


def test_every_event_a_handler_reads_is_installed() -> None:
    from mycelium_cli import reset_shadow, session_hook

    installed = set(session_cmd.hooks_block("mycelium-hook"))

    assert session_hook.READS <= installed, session_hook.READS - installed
    assert reset_shadow.EVENTS <= installed, reset_shadow.EVENTS - installed


def test_apply_keeps_a_copy_and_refuses_a_file_it_cannot_read(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"hooks": {}}', encoding="utf-8")
    r = runner.invoke(app, ["session", "install-hooks", "--apply", "--settings", str(path)])
    assert r.exit_code == 0, r.output
    assert "PostToolUse" in json.loads(path.read_text(encoding="utf-8"))["hooks"]
    assert len(list(tmp_path.glob("settings.json.bak-*"))) == 1

    path.write_text("{not json", encoding="utf-8")
    r = runner.invoke(app, ["session", "install-hooks", "--apply", "--settings", str(path)])
    assert r.exit_code != 0
    assert path.read_text(encoding="utf-8") == "{not json"


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "words"), [(5, "5 s"), (125, "2 min"), (3 * 3600 + 900, "3 h"), (2 * 86400, "2 d")]
)
def test_durations_are_said_at_a_glance(seconds: float, words: str) -> None:
    assert session_cmd.duration(seconds) == words


def test_a_row_says_what_the_person_would_do_about_it() -> None:
    now = dt.datetime(2026, 11, 26, 18, 0, tzinfo=dt.UTC)
    since = (now - dt.timedelta(minutes=12)).isoformat()
    heard = (now - dt.timedelta(minutes=25)).isoformat()

    def row(**kw: Any) -> dict[str, Any]:
        return {"attention_since": since, "heard_at": heard, "group": "needs_you", **kw}

    words = session_cmd.state_words
    assert words(row(attention="blocked", blocked_on="permission"), now) == (
        "waiting for a permission · 12 min"
    )
    assert words(row(attention="blocked", blocked_on="question"), now) == (
        "waiting for an answer · 12 min"
    )
    assert words(row(attention="your_turn"), now) == "finished, your turn · 12 min"
    assert words(row(attention="working", group="working"), now) == "working · 12 min"
    assert words(row(attention="working", group="quiet"), now) == "no news for 25 min"
    assert words(
        {"attention": None, "attention_since": None, "heard_at": heard, "group": "working"}, now
    ) == ("not reporting · seen 25 min ago")


# ---------------------------------------------------------------------------
# The shell's worker
# ---------------------------------------------------------------------------


def test_a_stale_session_key_does_not_cost_the_shell_its_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import contextlib

    calls: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "join_key" in body:
            return httpx.Response(404, json={"code": "session.join_key_unknown", "detail": "stale"})
        return httpx.Response(200, json={"id": "77777777-7777-7777-7777-777777777777"})

    @contextlib.contextmanager
    def stub() -> Any:
        with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://t") as c:
            yield c

    monkeypatch.setenv(worker_cmd.SESSION_KEY_VAR, "old-key")
    monkeypatch.setattr(worker_cmd, "client", stub)

    r = runner.invoke(app, ["worker", "open"])

    assert r.exit_code == 0, r.output
    assert calls == [{"join_key": "old-key"}, {}]
    assert "export MYCELIUM_WORKER=77777777-7777-7777-7777-777777777777" in r.stdout


def test_reconcile_forgets_only_what_the_server_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session that has just started may not be in the registry yet; its
    state file holds the agent's join key and must survive. Observed failing
    end to end on 2026-11-26 when unlisted files were dropped: the next
    reopening issued a new key and the agent's was refused as stale."""
    import contextlib

    agent_host.save_state(agent_host.HookState("new", "sid-new", "your_turn:", join_key="k"))
    agent_host.save_state(agent_host.HookState("dead", "sid-dead", "working:", join_key="j"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["alive"] == []
        return httpx.Response(200, json={"confirmed": [], "closed": ["sid-dead"]})

    @contextlib.contextmanager
    def stub(**_k: Any) -> Any:
        with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://t") as c:
            yield c

    monkeypatch.setattr(sync, "client", stub)

    assert sync.reconcile(force=True, live=[]) is True

    assert set(agent_host.all_states()) == {"new"}


def test_the_command_the_web_page_tells_a_person_to_run_exists() -> None:
    """The empty sessions page shows a command to copy. If the CLI renamed it,
    the page would hand out a command that fails, and nothing else would
    notice: read it from the page's source and run it."""
    import re
    import shlex

    page = Path(__file__).resolve().parents[2] / "web" / "src" / "routes" / "SessionsRoute.tsx"
    m = re.search(r"INSTALL_COMMAND = '([^']+)'", page.read_text(encoding="utf-8"))
    assert m is not None, "the page no longer declares INSTALL_COMMAND"
    argv = shlex.split(m.group(1))
    assert argv[0] == "mycelium"
    r = runner.invoke(app, [*argv[1:], "--help"])
    assert r.exit_code == 0, r.output
    for flag in (a for a in argv[1:] if a.startswith("--")):
        assert flag in r.output
