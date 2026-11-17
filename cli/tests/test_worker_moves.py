"""``mycelium worker`` and the task moves made as one, against a stubbed API.

The server treats the CLI's token as an agent's (docs/adr/0072), so a move
is refused unless the caller holds the task as a worker. What the CLI owns
is the protocol: it moves only as a worker, takes the task for that worker
first unless it already holds it, names the worker on the move, gives back
a hold it took when the move is refused, and prints the worker's export on
stdout and nothing else there.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
from typing import Any

import httpx
from typer.testing import CliRunner

from mycelium_cli.cmds import tasks as tasks_cmd
from mycelium_cli.cmds import worker as worker_cmd
from mycelium_cli.http import CLIError
from mycelium_cli.main import app

runner = CliRunner()

_TASK = "11111111-1111-1111-1111-111111111111"
_TODO = "22222222-2222-2222-2222-222222222222"
_DOING = "33333333-3333-3333-3333-333333333333"
_ME = "55555555-5555-5555-5555-555555555555"
_OTHER = "66666666-6666-6666-6666-666666666666"


_LATER = (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=30)).isoformat()
_EARLIER = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=30)).isoformat()


def _stub(
    cap: dict[str, Any],
    *,
    held_by: str | None = None,
    expires_at: str = _LATER,
    move_status: int = 200,
    acquire_status: int = 200,
):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        cap.setdefault("calls", []).append((request.method, request.url.path, body))
        path = request.url.path
        if path == f"/tasks/{_TASK}" and request.method == "GET":
            return httpx.Response(200, json={"id": _TASK, "state_id": _TODO, "version": 3})
        if path == f"/tasks/{_TASK}/states":
            return httpx.Response(
                200, json=[{"id": _TODO, "name": "todo"}, {"id": _DOING, "name": "in_progress"}]
            )
        if path == f"/tasks/{_TASK}/leases" and request.method == "GET":
            rows = [{"holder_worker_id": held_by, "expires_at": expires_at}] if held_by else []
            return httpx.Response(200, json=rows)
        if path == f"/tasks/{_TASK}/leases" and request.method == "POST":
            if acquire_status != 200:
                return httpx.Response(
                    acquire_status, json={"code": "worker.not_found", "message": "no worker"}
                )
            return httpx.Response(200, json={"holder_worker_id": (body or {})["worker_id"]})
        if path == f"/tasks/{_TASK}/state":
            if move_status != 200:
                return httpx.Response(
                    move_status,
                    json={"code": "task.close.requires_check", "message": "needs a check"},
                )
            return httpx.Response(200, json={"id": _TASK, "version": 4})
        if path == f"/tasks/{_TASK}/leases/release":
            return httpx.Response(200, json={})
        if path == "/tasks/workers" and request.method == "POST":
            return httpx.Response(200, json={"id": _ME, "label": None})
        if path == f"/tasks/workers/{_ME}/close":
            return httpx.Response(200, json={"worker": {"id": _ME}, "released_tasks": [_TASK]})
        return httpx.Response(404, json={"detail": f"unstubbed {request.method} {path}"})

    @contextlib.contextmanager
    def _cm(*_a: Any, **_k: Any):
        with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as c:
            yield c

    return _cm


def _posts(cap: dict[str, Any]) -> list[tuple[str, Any]]:
    return [(p, b) for m, p, b in cap.get("calls", []) if m == "POST"]


def test_a_move_without_a_worker_is_refused_before_any_call(monkeypatch) -> None:
    monkeypatch.delenv(worker_cmd.ENV_VAR, raising=False)
    cap: dict[str, Any] = {}
    monkeypatch.setattr(tasks_cmd, "client", _stub(cap))
    res = runner.invoke(app, ["task", "to", _TASK, "in_progress"])
    assert res.exit_code == 1
    assert isinstance(res.exception, CLIError)
    assert "worker open" in (res.exception.hint or "")
    assert "calls" not in cap


def test_a_move_takes_the_task_for_the_worker_then_names_it(monkeypatch) -> None:
    monkeypatch.setenv(worker_cmd.ENV_VAR, _ME)
    cap: dict[str, Any] = {}
    monkeypatch.setattr(tasks_cmd, "client", _stub(cap))
    res = runner.invoke(app, ["task", "to", _TASK, "in_progress"])
    assert res.exit_code == 0, res.output
    assert _posts(cap) == [
        (f"/tasks/{_TASK}/leases", {"worker_id": _ME}),
        (
            f"/tasks/{_TASK}/state",
            {"state_id": _DOING, "expected_version": 3, "worker_id": _ME},
        ),
    ]


def test_the_flag_wins_over_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(worker_cmd.ENV_VAR, _OTHER)
    cap: dict[str, Any] = {}
    monkeypatch.setattr(tasks_cmd, "client", _stub(cap))
    res = runner.invoke(app, ["task", "to", _TASK, "in_progress", "--worker", _ME])
    assert res.exit_code == 0, res.output
    assert all(b["worker_id"] == _ME for _, b in _posts(cap))


def test_a_task_already_held_is_taken_again_which_extends_it(monkeypatch) -> None:
    # The server, not the list, decides whether the worker holds it, and
    # taking it again is idempotent for the same worker.
    monkeypatch.setenv(worker_cmd.ENV_VAR, _ME)
    cap: dict[str, Any] = {}
    monkeypatch.setattr(tasks_cmd, "client", _stub(cap, held_by=_ME))
    res = runner.invoke(app, ["task", "to", _TASK, "in_progress"])
    assert res.exit_code == 0, res.output
    assert [p for p, _ in _posts(cap)] == [f"/tasks/{_TASK}/leases", f"/tasks/{_TASK}/state"]


def test_a_refused_move_gives_back_the_hold_it_took(monkeypatch) -> None:
    monkeypatch.setenv(worker_cmd.ENV_VAR, _ME)
    cap: dict[str, Any] = {}
    monkeypatch.setattr(tasks_cmd, "client", _stub(cap, move_status=409))
    res = runner.invoke(app, ["task", "to", _TASK, "in_progress"])
    assert res.exit_code == 1
    assert "task.close.requires_check" in str(res.exception)
    assert _posts(cap)[-1] == (f"/tasks/{_TASK}/leases/release", {"worker_id": _ME})


def test_a_refused_move_keeps_a_hold_it_did_not_take(monkeypatch) -> None:
    # The worker held it before this command: giving it back would undo
    # possession the session took on purpose. Written in capitals, the
    # same id is still the same worker.
    monkeypatch.setenv(worker_cmd.ENV_VAR, _ME.upper())
    cap: dict[str, Any] = {}
    monkeypatch.setattr(tasks_cmd, "client", _stub(cap, held_by=_ME, move_status=409))
    res = runner.invoke(app, ["task", "to", _TASK, "in_progress"])
    assert res.exit_code == 1
    assert [p for p, _ in _posts(cap)] == [f"/tasks/{_TASK}/leases", f"/tasks/{_TASK}/state"]


def test_an_expired_hold_is_no_hold(monkeypatch) -> None:
    # An unreleased lease past its deadline is still listed; it is taken
    # again, and a refused move gives that back too.
    monkeypatch.setenv(worker_cmd.ENV_VAR, _ME)
    cap: dict[str, Any] = {}
    monkeypatch.setattr(
        tasks_cmd, "client", _stub(cap, held_by=_ME, expires_at=_EARLIER, move_status=409)
    )
    res = runner.invoke(app, ["task", "to", _TASK, "in_progress"])
    assert res.exit_code == 1
    assert [p for p, _ in _posts(cap)] == [
        f"/tasks/{_TASK}/leases",
        f"/tasks/{_TASK}/state",
        f"/tasks/{_TASK}/leases/release",
    ]


def test_a_closed_worker_is_named_with_the_shell_repair(monkeypatch) -> None:
    monkeypatch.setenv(worker_cmd.ENV_VAR, _ME)
    cap: dict[str, Any] = {}
    monkeypatch.setattr(tasks_cmd, "client", _stub(cap, acquire_status=404))
    res = runner.invoke(app, ["task", "to", _TASK, "in_progress"])
    assert res.exit_code == 1
    assert isinstance(res.exception, CLIError)
    assert "worker open" in (res.exception.hint or "")
    assert [p for p, _ in _posts(cap)] == [f"/tasks/{_TASK}/leases"]


def test_a_worker_that_is_not_an_id_is_refused_before_any_call(monkeypatch) -> None:
    monkeypatch.setenv(worker_cmd.ENV_VAR, "term-1")
    cap: dict[str, Any] = {}
    monkeypatch.setattr(tasks_cmd, "client", _stub(cap))
    res = runner.invoke(app, ["task", "to", _TASK, "in_progress"])
    assert res.exit_code == 1
    assert "not a worker id" in str(res.exception)
    assert "calls" not in cap


def test_worker_open_prints_only_the_export_on_stdout(monkeypatch) -> None:
    cap: dict[str, Any] = {}
    monkeypatch.setattr(worker_cmd, "client", _stub(cap))
    res = runner.invoke(app, ["worker", "open", "--label", "term-1"])
    assert res.exit_code == 0, res.output
    assert res.stdout == f"export MYCELIUM_WORKER={_ME}\n"
    assert _posts(cap) == [("/tasks/workers", {"label": "term-1"})]


def test_worker_close_defaults_to_this_shells_worker_and_unsets_it(monkeypatch) -> None:
    monkeypatch.setenv(worker_cmd.ENV_VAR, _ME)
    cap: dict[str, Any] = {}
    monkeypatch.setattr(worker_cmd, "client", _stub(cap))
    res = runner.invoke(app, ["worker", "close"])
    assert res.exit_code == 0, res.output
    assert res.stdout == "unset MYCELIUM_WORKER\n"
    assert [p for p, _ in _posts(cap)] == [f"/tasks/workers/{_ME}/close"]
