"""``mycelium workflow export`` against a stubbed API (offline).

The rules of the document live in the server (docs/adr/0052), so what is
worth testing here is the part the CLI actually owns: which endpoint it
calls, and that the file it writes is the server's bytes rather than a
re-serialisation of them. Import is not offered (docs/adr/0072).
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import httpx
from typer.testing import CliRunner

from mycelium_cli.cmds import workflows as wf_cmd
from mycelium_cli.main import app

runner = CliRunner()

_WF = "44444444-4444-4444-4444-444444444444"

# Deliberately NOT re-indented: the test asserts these exact bytes reach
# the file, which is how a second export of an unchanged workflow stays
# byte-identical to the first.
_EXPORTED = (
    '{\n  "kind": "mycelium.workflow",\n  "version": 1,\n  "name": "Delivery",\n'
    '  "description": null,\n  "states": [\n    {\n      "name": "todo",\n'
    '      "is_initial": true,\n      "is_terminal": false,\n      "is_hidden": false,\n'
    '      "description": null\n    }\n  ],\n  "transitions": []\n}\n'
)


def _stub_client(capture: dict[str, Any]):
    def handler(request: httpx.Request) -> httpx.Response:
        capture.setdefault("calls", []).append((request.method, request.url.path))
        capture["query"] = dict(request.url.params)
        if request.url.path == "/workflows" and request.method == "GET":
            return httpx.Response(200, json=[{"id": _WF, "name": "Delivery", "is_default": False}])
        if request.url.path.endswith("/export"):
            return httpx.Response(200, text=_EXPORTED, headers={"content-type": "application/json"})
        capture["body"] = json.loads(request.content)
        if request.url.path == "/workflows/import":
            return httpx.Response(200, json={"id": _WF, "name": "Delivery"})
        return httpx.Response(204)

    @contextlib.contextmanager
    def _cm(*_a: Any, **_k: Any):
        with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as c:
            yield c

    return _cm


def test_export_writes_the_servers_bytes_verbatim(monkeypatch, tmp_path: Path) -> None:
    cap: dict[str, Any] = {}
    monkeypatch.setattr(wf_cmd, "client", _stub_client(cap))
    out = tmp_path / "wf.json"
    res = runner.invoke(app, ["workflow", "export", _WF, "--file", str(out)])
    assert res.exit_code == 0, res.output
    assert ("GET", f"/workflows/{_WF}/export") in cap["calls"]
    assert out.read_text(encoding="utf-8") == _EXPORTED


def test_export_accepts_an_id_prefix_and_names_the_file_after_the_workflow(
    monkeypatch, tmp_path: Path
) -> None:
    cap: dict[str, Any] = {}
    monkeypatch.setattr(wf_cmd, "client", _stub_client(cap))
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["workflow", "export", "4444"])
    assert res.exit_code == 0, res.output
    # The prefix was resolved through the listing before the export.
    assert ("GET", "/workflows") in cap["calls"]
    assert (tmp_path / "workflow-Delivery.json").read_text(encoding="utf-8") == _EXPORTED


def test_export_to_stdout(monkeypatch) -> None:
    monkeypatch.setattr(wf_cmd, "client", _stub_client({}))
    res = runner.invoke(app, ["workflow", "export", _WF, "--file", "-"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["name"] == "Delivery"


def test_there_is_no_import_a_workflow_is_written_from_the_web_app() -> None:
    # The CLI's token is an agent's to the server, and writing a workflow
    # is a person's act; a command that can only ever be refused is not
    # offered.
    res = runner.invoke(app, ["workflow", "import", "--file", "wf.json"])
    assert res.exit_code != 0
    assert "No such command" in res.output
