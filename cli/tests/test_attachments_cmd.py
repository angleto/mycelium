"""``mycelium attachments download-capability`` against a stubbed API (offline).

The command mints a parent-scoped capability token and prints a ready
``curl -o`` per file, each carrying the ephemeral token (no PAT, no
X-Workspace-Id). The API is stubbed with httpx.MockTransport so no backend
is needed; the POST body is captured to assert the params are forwarded.
"""

from __future__ import annotations

import contextlib
import json

import httpx
from typer.testing import CliRunner

from mycelium_cli.cmds import attachments as att_cmd
from mycelium_cli.main import app

runner = CliRunner()

_TASK = "33333333-3333-3333-3333-333333333333"
_PAYLOAD = {
    "token": "mycelium_cap_" + "a" * 43,
    "expires_at": "2026-06-09T12:00:00+00:00",
    "parent_kind": "task",
    "parent_id": _TASK,
    "attachments": [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "note_id": None,
            "task_id": _TASK,
            "filename": "PDL GIUGNO.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 12,
            "created_at": "2026-06-01T00:00:00+00:00",
        },
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "note_id": None,
            "task_id": _TASK,
            "filename": "PDL MAGGIO.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 9,
            "created_at": "2026-05-01T00:00:00+00:00",
        },
    ],
}


def _stub_client(capture: dict, response: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        capture["method"] = request.method
        capture["path"] = request.url.path
        capture["body"] = json.loads(request.content)
        return httpx.Response(201, json=response)

    @contextlib.contextmanager
    def _cm(*_a, **_k):
        with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as c:
            yield c

    return _cm


def test_download_capability_prints_a_curl_per_file(monkeypatch) -> None:
    cap: dict = {}
    monkeypatch.setattr(att_cmd, "client", _stub_client(cap, _PAYLOAD))
    res = runner.invoke(app, ["attachments", "download-capability", "task", _TASK, "--ttl", "120"])
    assert res.exit_code == 0, res.output
    # Params forwarded; a full UUID skips the resolve list call.
    assert cap["method"] == "POST"
    assert cap["path"] == "/attachments/capability"
    assert cap["body"] == {"parent_kind": "task", "parent_id": _TASK, "ttl_seconds": 120}
    # One ready curl per attachment, carrying the ephemeral token + -o name.
    assert "mycelium_cap_" + "a" * 43 in res.output
    assert "http://test/attachments/11111111-1111-1111-1111-111111111111/download" in res.output
    assert "-o 'PDL GIUGNO.pdf'" in res.output
    assert "-o 'PDL MAGGIO.pdf'" in res.output


def test_download_capability_json_includes_built_curls(monkeypatch) -> None:
    cap: dict = {}
    monkeypatch.setattr(att_cmd, "client", _stub_client(cap, _PAYLOAD))
    res = runner.invoke(app, ["--json", "attachments", "download-capability", "task", _TASK])
    assert res.exit_code == 0, res.output
    assert cap["body"]["ttl_seconds"] == 300  # default
    payload = json.loads(res.output)
    assert payload["token"].startswith("mycelium_cap_")
    assert payload["attachments"][0]["curl"].startswith("curl -fsS ")


def test_download_capability_rejects_bad_parent_kind() -> None:
    # Validated before any network call, so no stub is needed.
    res = runner.invoke(app, ["attachments", "download-capability", "folder", _TASK])
    assert res.exit_code != 0
