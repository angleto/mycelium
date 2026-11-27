"""``mycelium notif send`` against a stubbed API (offline).

The HTTP client is the network edge of the CLI, so it is replaced by an
``httpx.MockTransport`` and the request it receives is the contract: the path,
and a body that names no recipient. What the server does with it is covered
end to end in api/tests/test_notify_self.py.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

import httpx
from typer.testing import CliRunner

from mycelium_cli.cmds import notifications as notif_cmd
from mycelium_cli.main import app

runner = CliRunner()


def _stub(capture: dict[str, Any], status: int, response: dict[str, Any]):
    def handler(request: httpx.Request) -> httpx.Response:
        capture["path"] = request.url.path
        capture["body"] = json.loads(request.content)
        return httpx.Response(status, json=response)

    @contextlib.contextmanager
    def _cm(*_a: Any, **_k: Any):
        with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as c:
            yield c

    return _cm


def test_send_posts_title_body_and_key_and_nothing_else(monkeypatch) -> None:
    cap: dict[str, Any] = {}
    monkeypatch.setattr(
        notif_cmd,
        "client",
        _stub(cap, 200, {"queued": ["email", "telegram"], "already_queued": []}),
    )
    r = runner.invoke(app, ["notif", "send", "AC unplugged", "-b", "battery 40%", "-k", "ac-1"])
    assert r.exit_code == 0, r.output
    assert cap["path"] == "/notifications/self"
    assert cap["body"] == {"title": "AC unplugged", "body": "battery 40%", "key": "ac-1"}


def test_send_without_key_sends_no_key(monkeypatch) -> None:
    cap: dict[str, Any] = {}
    monkeypatch.setattr(
        notif_cmd, "client", _stub(cap, 200, {"queued": ["email"], "already_queued": []})
    )
    r = runner.invoke(app, ["notif", "send", "hello"])
    assert r.exit_code == 0, r.output
    assert cap["body"] == {"title": "hello", "body": ""}


def test_send_reports_a_refusal_and_fails(monkeypatch) -> None:
    cap: dict[str, Any] = {}
    monkeypatch.setattr(
        notif_cmd,
        "client",
        _stub(
            cap,
            422,
            {
                "code": "notification.no_reachable_channel",
                "detail": "No channel of yours can be reached in this workspace",
                "correlation_id": "c1",
            },
        ),
    )
    r = runner.invoke(app, ["notif", "send", "anyone?"])
    # The refusal surfaces as the CLI's own error, carrying the server's code
    # and detail; the entry point prints it (covered by test_http_errors.py).
    assert r.exit_code != 0
    assert "notification.no_reachable_channel" in str(r.exception)
    assert "No channel of yours" in str(r.exception)
