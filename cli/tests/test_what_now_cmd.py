"""``mycelium what-now`` behaviour against a stubbed API (offline).

Covers the NarratedPlanOut envelope read (T4/T12) and the new selection +
narrate flags. The API is stubbed with httpx.MockTransport so no backend is
needed; the request body is captured to assert the params are forwarded.
"""

from __future__ import annotations

import contextlib
import io
import json

import httpx
from rich.console import Console
from typer.testing import CliRunner

from mycelium_cli import ui
from mycelium_cli.cmds import what_now as wn
from mycelium_cli.main import app

runner = CliRunner()

_ENVELOPE = {
    "ranked": [
        {
            "task_id": "11111111-1111-1111-1111-111111111111",
            "title": "Fix boiler",
            "necessity": "must",
            "priority": 1,
            "due_date": None,
            "remaining_minutes": 30,
            "slack_minutes": None,
            "deadline_bucket": "none",
        }
    ],
    "narration": None,
    "narration_model": None,
    "narrated": False,
}


def _stub_client(capture: dict, response: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        capture["body"] = json.loads(request.content)
        return httpx.Response(200, json=response)

    @contextlib.contextmanager
    def _cm(*_a, **_k):
        with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as c:
            yield c

    return _cm


def _capture_console(monkeypatch) -> io.StringIO:
    # Bind the stdout Console to a wide buffer so the Rich table never wraps
    # and the rendered text is greppable, independent of CliRunner's stdout.
    buf = io.StringIO()
    monkeypatch.setattr(ui, "_console", Console(file=buf, width=300, no_color=True))
    return buf


def test_what_now_reads_envelope_and_forwards_selection(monkeypatch) -> None:
    cap: dict = {}
    monkeypatch.setattr(wn, "client", _stub_client(cap, _ENVELOPE))
    buf = _capture_console(monkeypatch)
    res = runner.invoke(
        app,
        [
            "what-now",
            "-d",
            "60",
            "--min-priority",
            "5",
            "--focus-tag",
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "--tag",
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "--min-necessity",
            "should",
            "--narrate",
        ],
    )
    assert res.exit_code == 0, res.output
    body = cap["body"]
    assert body["duration_minutes"] == 60
    assert body["min_priority"] == 5
    assert body["min_necessity"] == "should"
    assert body["narrate"] is True
    assert body["focus_tag_ids"] == ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]
    assert body["any_tag_ids"] == ["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"]
    # window_start defaults to now() and is timezone-aware (offset present).
    assert "+" in body["window_start"] or body["window_start"].endswith("Z")
    text = buf.getvalue()
    # Table rendered from ['ranked'] (no longer a bare list).
    assert "Fix boiler" in text
    # narrated=false -> a dim 'unavailable' line, not an error.
    assert "unavailable" in text.lower()


def test_what_now_unset_selectors_are_omitted(monkeypatch) -> None:
    cap: dict = {}
    monkeypatch.setattr(wn, "client", _stub_client(cap, _ENVELOPE))
    _capture_console(monkeypatch)
    res = runner.invoke(app, ["what-now", "-d", "45"])
    assert res.exit_code == 0, res.output
    body = cap["body"]
    # Empty selectors are not sent at all (omitted, not [] / null).
    for k in ("focus_tag_ids", "any_tag_ids", "min_priority", "min_necessity"):
        assert k not in body
    assert body["narrate"] is False


def test_what_now_json_emits_full_envelope(monkeypatch) -> None:
    cap: dict = {}
    monkeypatch.setattr(wn, "client", _stub_client(cap, _ENVELOPE))
    res = runner.invoke(app, ["--json", "what-now", "-d", "60"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["narrated"] is False
    assert payload["ranked"][0]["title"] == "Fix boiler"


def test_what_now_invalid_min_necessity_errors(monkeypatch) -> None:
    monkeypatch.setattr(wn, "client", _stub_client({}, _ENVELOPE))
    res = runner.invoke(app, ["what-now", "--min-necessity", "urgent"])
    assert res.exit_code != 0
