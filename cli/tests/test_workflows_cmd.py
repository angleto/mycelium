"""``mycelium workflow export|import`` against a stubbed API (offline).

The rules of the document live in the server (docs/adr/0052), so what is
worth testing here is the part the CLI actually owns: which endpoint it
calls, that the file it writes is the server's bytes rather than a
re-serialisation of them, that ``--into`` is what separates "replace
this workflow" from "create a new one", and that a file which is not
JSON fails with a message naming the file instead of a traceback.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import httpx
from typer.testing import CliRunner

from mycelium_cli.cmds import workflows as wf_cmd
from mycelium_cli.http import CLIError
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


def test_import_without_into_creates_a_new_workflow(monkeypatch, tmp_path: Path) -> None:
    cap: dict[str, Any] = {}
    monkeypatch.setattr(wf_cmd, "client", _stub_client(cap))
    src = tmp_path / "wf.json"
    src.write_text(_EXPORTED, encoding="utf-8")
    res = runner.invoke(app, ["workflow", "import", "--file", str(src), "--name", "Copy"])
    assert res.exit_code == 0, res.output
    assert ("POST", "/workflows/import") in cap["calls"]
    assert cap["query"]["name"] == "Copy"
    assert cap["body"] == json.loads(_EXPORTED)


def test_import_with_into_replaces_that_workflow(monkeypatch, tmp_path: Path) -> None:
    cap: dict[str, Any] = {}
    monkeypatch.setattr(wf_cmd, "client", _stub_client(cap))
    src = tmp_path / "wf.json"
    src.write_text(_EXPORTED, encoding="utf-8")
    res = runner.invoke(app, ["workflow", "import", "--file", str(src), "--into", _WF])
    assert res.exit_code == 0, res.output
    assert ("POST", f"/workflows/{_WF}/import") in cap["calls"]
    assert ("POST", "/workflows/import") not in cap["calls"]


def test_name_with_into_is_refused_rather_than_ignored(monkeypatch, tmp_path: Path) -> None:
    # Accepting it silently would look like a rename that never happened.
    cap: dict[str, Any] = {}
    monkeypatch.setattr(wf_cmd, "client", _stub_client(cap))
    src = tmp_path / "wf.json"
    src.write_text(_EXPORTED, encoding="utf-8")
    res = runner.invoke(
        app, ["workflow", "import", "--file", str(src), "--into", _WF, "--name", "Copy"]
    )
    # CliRunner invokes the Typer app directly, so the CLIError arrives
    # as the exception; main._entrypoint is what renders it in a shell.
    assert res.exit_code == 1
    assert isinstance(res.exception, CLIError)
    assert "--name" in str(res.exception)
    assert "calls" not in cap


def test_a_file_that_is_not_json_fails_with_the_filename(monkeypatch, tmp_path: Path) -> None:
    cap: dict[str, Any] = {}
    monkeypatch.setattr(wf_cmd, "client", _stub_client(cap))
    src = tmp_path / "notes.txt"
    src.write_text("this is not a workflow", encoding="utf-8")
    res = runner.invoke(app, ["workflow", "import", "--file", str(src)])
    assert res.exit_code == 1
    assert isinstance(res.exception, CLIError)
    assert "notes.txt" in str(res.exception)
    # Nothing was sent: a bad file is caught before the round trip.
    assert "calls" not in cap
