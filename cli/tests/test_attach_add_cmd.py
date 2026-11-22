"""``mycelium task attach add`` / ``note attach add`` with several files (offline).

The command uploads each file in order and prints the ``markdown_ref`` the
server built for it; the CLI no longer builds references itself. The API is
stubbed with httpx.MockTransport, which answers like the real upload route:
the attachment JSON, ``markdown_ref`` included.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import httpx
from typer.testing import CliRunner

from mycelium_cli.cmds import notes as notes_cmd
from mycelium_cli.cmds import tasks as tasks_cmd
from mycelium_cli.http import CLIError
from mycelium_cli.main import app

runner = CliRunner()

_TASK = "33333333-3333-3333-3333-333333333333"
_NOTE = "44444444-4444-4444-4444-444444444444"


def _stub_client(seen: list[dict], *, fail_on: str | None = None, with_ref: bool = True):
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("latin-1")
        name = body.split('filename="', 1)[1].split('"', 1)[0]
        seen.append({"path": request.url.path, "filename": name})
        if name == fail_on:
            return httpx.Response(
                400, json={"code": "attachment.too_large", "detail": "too large", "params": {}}
            )
        att_id = f"id-{len(seen)}"
        out = {"id": att_id, "filename": name, "mime_type": "application/pdf", "size_bytes": 3}
        if with_ref:
            out["markdown_ref"] = f"[{name}](/attachments/{att_id}/download)"
        return httpx.Response(200, json=out)

    @contextlib.contextmanager
    def _cm(*_a, **_k):
        with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as c:
            yield c

    return _cm


def _files(tmp_path: Path, *names: str) -> list[str]:
    out = []
    for n in names:
        p = tmp_path / n
        p.write_bytes(b"%PD")
        out.append(str(p))
    return out


def test_task_attach_add_uploads_every_file_and_prints_its_ref(monkeypatch, tmp_path) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(tasks_cmd, "client", _stub_client(seen))
    paths = _files(tmp_path, "a.pdf", "b.pdf", "c.pdf")
    res = runner.invoke(app, ["task", "attach", "add", _TASK, *paths])
    assert res.exit_code == 0, res.output
    assert [s["filename"] for s in seen] == ["a.pdf", "b.pdf", "c.pdf"]
    assert {s["path"] for s in seen} == {f"/tasks/{_TASK}/attachments"}
    for i, n in enumerate(["a.pdf", "b.pdf", "c.pdf"], start=1):
        assert f"markdown: [{n}](/attachments/id-{i}/download)" in res.output


def test_note_attach_add_json_is_the_list_of_uploads(monkeypatch, tmp_path) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(notes_cmd, "client", _stub_client(seen))
    paths = _files(tmp_path, "x.pdf", "y.pdf")
    res = runner.invoke(app, ["--json", "note", "attach", "add", _NOTE, *paths])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert [a["filename"] for a in payload] == ["x.pdf", "y.pdf"]
    assert {s["path"] for s in seen} == {f"/notes/{_NOTE}/attachments"}


def test_a_failed_upload_stops_and_names_what_already_landed(monkeypatch, tmp_path) -> None:
    """Uploads are not idempotent, so a retry must know what not to resend."""
    seen: list[dict] = []
    monkeypatch.setattr(tasks_cmd, "client", _stub_client(seen, fail_on="b.pdf"))
    paths = _files(tmp_path, "a.pdf", "b.pdf", "c.pdf")
    res = runner.invoke(app, ["task", "attach", "add", _TASK, *paths])
    assert res.exit_code != 0
    assert [s["filename"] for s in seen] == ["a.pdf", "b.pdf"]
    # The app's entry point renders a CLIError; under CliRunner it surfaces as
    # the exception, message and hint intact.
    assert isinstance(res.exception, CLIError)
    assert "b.pdf" in str(res.exception)
    assert res.exception.hint == "already uploaded: a.pdf"


def test_a_server_without_markdown_ref_is_an_error_not_a_guess(monkeypatch, tmp_path) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(tasks_cmd, "client", _stub_client(seen, with_ref=False))
    res = runner.invoke(app, ["task", "attach", "add", _TASK, *_files(tmp_path, "a.pdf")])
    assert res.exit_code != 0
    assert isinstance(res.exception, CLIError)
    assert "markdown_ref" in str(res.exception)
