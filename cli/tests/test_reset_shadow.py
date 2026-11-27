"""The reset shadow (docs/adr/0081): the rule, the recorder in the hook, and
the summary. Offline: no server, no Claude Code."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from mycelium_cli import reset_shadow, session_hook
from mycelium_cli import session_sync as sync
from mycelium_cli.main import app
from mycelium_cli.reset_rule import (
    PARAMETERS,
    Action,
    Basis,
    Observation,
    Parameters,
    Quality,
    recommend,
)

runner = CliRunner()
OPUS = "claude-opus-5-5"


@pytest.fixture(autouse=True)
def _state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MYCELIUM_STATE_DIR", str(tmp_path / "state"))
    # The attention report is not under test here and must not reach a server.
    monkeypatch.setattr(sync, "logged_in", lambda: False)
    return tmp_path / "state"


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def _obs(context: int, model: str | None = OPUS, failed: bool = False) -> Observation:
    return Observation(model=model, context=context, failed_since_last_prompt=failed)


def test_a_short_context_continues_and_a_long_one_compacts() -> None:
    short = recommend(_obs(60_000))
    long = recommend(_obs(300_000))
    assert short is not None and long is not None
    assert short.action is Action.CONTINUE and short.margin_usd == 0.0
    assert short.compact_margin_usd < 0
    assert long.action is Action.COMPACT and long.margin_usd > 0


def test_the_margin_is_the_cost_difference_computed_by_hand() -> None:
    # Opus 5.5: cache read 0.20, output 20, cache write 8 USD per million.
    # Continue: 124 * 300k * 0.20e-6 = 7.44.
    # Compact: 300k * 0.20e-6 + 17,720 * 20e-6 + 68,420 * 8e-6
    #   + 124 * 68,420 * 0.20e-6 = 0.06 + 0.3544 + 0.54736 + 1.696816 = 2.658576.
    rec = recommend(_obs(300_000))
    assert rec is not None
    assert rec.compact_margin_usd == pytest.approx(7.44 - 2.658576, abs=1e-4)


def test_the_break_even_sits_between_100k_and_110k_on_opus() -> None:
    # Where compacting starts to pay with the table of 2026-11-27; a change
    # of parameters that moves it shows here, with the version beside it.
    assert PARAMETERS.version == "2026-11-27.1"
    below = recommend(_obs(100_000))
    above = recommend(_obs(110_000))
    assert below is not None and above is not None
    assert below.action is Action.CONTINUE
    assert above.action is Action.COMPACT


def test_a_restart_and_a_delegation_are_never_recommended() -> None:
    actions = {r.action for c in range(0, 1_000_001, 10_000) if (r := recommend(_obs(c)))}
    assert actions <= {Action.CONTINUE, Action.COMPACT}


def test_the_basis_says_whether_quality_was_measured() -> None:
    opus = recommend(_obs(300_000))
    sonnet = recommend(_obs(300_000, model="claude-sonnet-5-5"))
    assert opus is not None and sonnet is not None
    assert opus.basis is Basis.COST_QUALITY_NULL
    assert sonnet.basis is Basis.COST_QUALITY_UNMEASURED


def test_a_model_without_a_price_gets_no_recommendation() -> None:
    assert recommend(_obs(300_000, model="some-other-model")) is None
    assert recommend(_obs(300_000, model=None)) is None


def test_measured_quality_terms_are_refused_rather_than_ignored() -> None:
    p = Parameters(
        version="t",
        fresh_context=PARAMETERS.fresh_context,
        compacted_context=PARAMETERS.compacted_context,
        summary_output=PARAMETERS.summary_output,
        remaining_calls=PARAMETERS.remaining_calls,
        prices=PARAMETERS.prices,
        quality={
            OPUS: Quality(null=False, anchoring_fail_upper=0.5, dilution_fail_upper=0.5, source="t")
        },
    )
    with pytest.raises(ValueError, match="no weighing"):
        recommend(_obs(300_000), p)


def test_a_failure_is_carried_into_the_recommendation() -> None:
    rec = recommend(_obs(60_000, failed=True))
    assert rec is not None and rec.after_failure


# ---------------------------------------------------------------------------
# The recorder
# ---------------------------------------------------------------------------


def _assistant(mid: str, context: int, model: str = OPUS, sidechain: bool = False) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "isSidechain": sidechain,
            "message": {
                "id": mid,
                "model": model,
                "usage": {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 1000,
                    "cache_read_input_tokens": context - 1002,
                },
            },
        }
    )


def _hook(payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr("sys.stdout", io.StringIO())
    return session_hook.main([])


def _records() -> list[dict[str, Any]]:
    return [json.loads(line) for line in reset_shadow.read_log()]


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)  # noqa: S603, S607  # a literal git argv against a temporary repository


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    _git(
        root,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "one",
    )
    return root


def _turn_on(root: Path) -> None:
    reset_shadow.allow(str(root))
    reset_shadow.set_enabled(True)


def _prompt(
    root: Path, transcript: Path, monkeypatch: pytest.MonkeyPatch, ref: str = "s-1"
) -> None:
    assert (
        _hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": ref,
                "cwd": str(root),
                "transcript_path": str(transcript),
            },
            monkeypatch,
        )
        == 0
    )


def test_nothing_is_recorded_for_a_project_not_allowed(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_assistant("m1", 300_000) + "\n")
    _prompt(project, transcript, monkeypatch)
    assert not reset_shadow.log_path().exists()
    reset_shadow.allow(str(tmp_path / "elsewhere"))
    reset_shadow.set_enabled(True)
    _prompt(project, transcript, monkeypatch)
    assert not reset_shadow.log_path().exists()


def test_an_allowed_project_records_numbers_and_no_text(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _turn_on(project)
    transcript = tmp_path / "t.jsonl"
    secret = "the prompt text must never reach the log"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"content": secret}})
        + "\n"
        + _assistant("m1", 120_000)
        + "\n"
        + _assistant("m1", 120_000)
        + "\n"  # a second content block of the same call
        + _assistant("m2", 300_000)
        + "\n"
        + _assistant("sub", 900_000, sidechain=True)
        + "\n"
    )
    (project / "sub").mkdir()
    _prompt(project / "sub", transcript, monkeypatch)  # a subdirectory of the allowed root
    (rec,) = _records()
    assert rec["event"] == "prompt"
    assert rec["calls"] == 2 and rec["context"] == 300_000 and rec["model"] == OPUS
    assert rec["recommended"] == "compact" and rec["basis"] == "cost_quality_null"
    assert rec["project"] == "proj"
    raw = reset_shadow.log_path().read_text()
    assert secret not in raw and str(project) not in raw


def test_the_transcript_is_read_from_where_the_last_prompt_left_it(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _turn_on(project)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_assistant("m1", 60_000) + "\n" + '{"type": "assistant", "mess')
    _prompt(project, transcript, monkeypatch)
    with transcript.open("a") as f:
        f.write('age": {}}\n' + _assistant("m2", 70_000) + "\n")
    _prompt(project, transcript, monkeypatch)
    _prompt(project, transcript, monkeypatch)  # nothing written since: nothing counted again
    first, second, third = _records()
    assert (first["calls"], first["context"]) == (1, 60_000)
    assert (second["calls"], second["context"], second["prompts"]) == (2, 70_000, 2)
    assert (third["calls"], third["context"], third["prompts"]) == (2, 70_000, 3)


def test_lines_before_the_offset_are_not_read_again(tmp_path: Path) -> None:
    # The counts alone cannot show a reread: a reader that starts from zero
    # every time finds the transcript "shorter than the offset" on the next
    # prompt, resets and counts the same total. What it costs is the reread,
    # so the test plants a call before the offset that must not be counted.
    transcript = tmp_path / "t.jsonl"
    before = _assistant("old", 500_000) + "\n"
    transcript.write_text(before + _assistant("new", 70_000) + "\n")
    progress = reset_shadow.Progress(offset=len(before.encode()))
    reset_shadow.read_transcript(str(transcript), progress)
    assert (progress.calls, progress.context) == (1, 70_000)
    assert progress.offset == transcript.stat().st_size


def test_a_commit_between_prompts_is_seen_as_head_moved(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _turn_on(project)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_assistant("m1", 60_000) + "\n")
    _prompt(project, transcript, monkeypatch)
    _prompt(project, transcript, monkeypatch)
    _git(
        project,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "two",
    )
    _prompt(project, transcript, monkeypatch)
    assert [r["head_moved"] for r in _records()] == [False, False, True]


def test_a_failure_marks_the_next_prompt_only(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _turn_on(project)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_assistant("m1", 60_000) + "\n")
    _hook(
        {"hook_event_name": "PostToolUseFailure", "session_id": "s-1", "cwd": str(project)},
        monkeypatch,
    )
    _prompt(project, transcript, monkeypatch)
    _prompt(project, transcript, monkeypatch)
    assert [r["after_failure"] for r in _records()] == [True, False]


def test_a_fault_in_the_shadow_does_not_stop_the_attention_report(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _turn_on(project)
    sent: list[tuple[Any, ...]] = []

    def broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("shadow broken")

    monkeypatch.setattr(reset_shadow, "observe", broken)
    monkeypatch.setattr(sync, "logged_in", lambda: True)
    monkeypatch.setattr(sync, "report", lambda *a: sent.append(a))
    code = _hook(
        {"hook_event_name": "UserPromptSubmit", "session_id": "s-1", "cwd": str(project)},
        monkeypatch,
    )
    assert code == 0
    assert sent and sent[0][2] == "working"


def test_the_shadow_imports_neither_the_http_client_nor_typer(
    project: Path, tmp_path: Path
) -> None:
    # The fast path of docs/adr/0078 is the hook's business; what the shadow
    # must not do is add to it. So the shadow is run on its own, in a fresh
    # interpreter, and what it loaded is read afterwards. (The hook's first
    # `working` report imports the client by design, so running the whole
    # hook here would measure that, not the shadow.)
    state = tmp_path / "state2"
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_assistant("m1", 60_000) + "\n")
    script = (
        "import json, sys\n"
        "from mycelium_cli import reset_shadow\n"
        f"reset_shadow.allow({str(project)!r})\n"
        "reset_shadow.set_enabled(True)\n"
        "reset_shadow.observe('UserPromptSubmit', {'session_id': 's', "
        f"'cwd': {str(project)!r}, 'transcript_path': {str(transcript)!r}}})\n"
        "print(json.dumps(['httpx' in sys.modules, 'typer' in sys.modules, "
        "reset_shadow.log_path().exists()]))\n"
    )
    r = subprocess.run(  # noqa: S603  # this interpreter, running a script the test wrote
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={"MYCELIUM_STATE_DIR": str(state), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        check=True,
    )
    httpx_loaded, typer_loaded, recorded = json.loads(r.stdout.strip().splitlines()[-1])
    assert recorded
    assert not typer_loaded
    assert not httpx_loaded


# ---------------------------------------------------------------------------
# The switches
# ---------------------------------------------------------------------------


def _one_prompt(project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_assistant("m1", 300_000) + "\n")
    _prompt(project, transcript, monkeypatch)
    return len(_records()) if reset_shadow.log_path().exists() else 0


def test_allowing_a_project_does_not_start_recording(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset_shadow.allow(str(project))
    assert reset_shadow.settings().enabled is False
    assert _one_prompt(project, tmp_path, monkeypatch) == 0


def test_off_stops_everywhere_and_keeps_the_projects(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _turn_on(project)
    assert _one_prompt(project, tmp_path, monkeypatch) == 1
    reset_shadow.set_enabled(False)
    assert _one_prompt(project, tmp_path, monkeypatch) == 1
    assert reset_shadow.allowed_roots() == [str(project.resolve())]
    reset_shadow.set_enabled(True)
    assert _one_prompt(project, tmp_path, monkeypatch) == 2


def test_the_environment_turns_one_session_off(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _turn_on(project)
    monkeypatch.setenv(reset_shadow.ENV_SWITCH, "off")
    assert _one_prompt(project, tmp_path, monkeypatch) == 0
    monkeypatch.setenv(reset_shadow.ENV_SWITCH, "on")
    assert _one_prompt(project, tmp_path, monkeypatch) == 1


def test_a_mistyped_session_switch_records_nothing_and_says_why(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _turn_on(project)
    monkeypatch.setenv(reset_shadow.ENV_SWITCH, "of")
    assert _one_prompt(project, tmp_path, monkeypatch) == 0
    from mycelium_cli import agent_host

    assert "MYCELIUM_RESET_SHADOW='of'" in agent_host.log_path().read_text()


def test_the_cli_turns_it_on_and_off_and_reports_the_switches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "p"
    root.mkdir()
    runner.invoke(app, ["session", "reset-shadow", "allow", str(root)])
    status = json.loads(runner.invoke(app, ["--json", "session", "reset-shadow", "status"]).stdout)
    assert status["enabled"] is False and status["roots"] == [str(root.resolve())]
    assert runner.invoke(app, ["session", "reset-shadow", "on"]).exit_code == 0
    assert reset_shadow.settings().enabled is True
    assert runner.invoke(app, ["session", "reset-shadow", "off"]).exit_code == 0
    assert reset_shadow.settings() == reset_shadow.Settings(False, (str(root.resolve()),))
    monkeypatch.setenv(reset_shadow.ENV_SWITCH, "off")
    status = json.loads(runner.invoke(app, ["--json", "session", "reset-shadow", "status"]).stdout)
    assert status["session_switch"] == "off"


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------


def _line(**kw: Any) -> str:
    return json.dumps({"session": "s", **kw})


def test_the_summary_pairs_each_prompt_with_what_followed() -> None:
    lines = [
        _line(event="prompt", basis="cost_quality_null", recommended="compact"),
        _line(event="delegated"),
        _line(event="compact", trigger="manual"),
        _line(event="prompt", basis="cost_quality_null", recommended="compact"),
        _line(event="delegated"),
        _line(event="prompt", basis="cost_quality_null", recommended="continue"),
        _line(event="prompt", basis="cost_quality_null", recommended="continue"),
    ]
    s = reset_shadow.summary(lines)
    pairs = {(p["recommended"], p["done"]): p["count"] for p in s["pairs"]}
    assert pairs == {
        ("compact", "compact_manual"): 1,
        ("compact", "delegate"): 1,
        ("continue", "continue"): 1,
    }
    assert s["prompts"] == 4 and s["pending"] == 1


def test_the_last_prompt_of_an_ended_session_is_not_pending() -> None:
    lines = [_line(event="prompt", basis="b", recommended="continue"), _line(event="session_end")]
    s = reset_shadow.summary(lines)
    assert s["pending"] == 0
    assert s["pairs"] == [{"basis": "b", "recommended": "continue", "done": "ended", "count": 1}]


def test_the_cli_allows_lists_and_denies(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    assert runner.invoke(app, ["session", "reset-shadow", "allow", str(root)]).exit_code == 0
    listed = runner.invoke(app, ["--json", "session", "reset-shadow", "status"])
    assert str(root.resolve()) in json.loads(listed.stdout)["roots"]
    assert runner.invoke(app, ["session", "reset-shadow", "deny", str(root)]).exit_code == 0
    assert reset_shadow.allowed_roots() == []
    assert runner.invoke(app, ["session", "reset-shadow", "deny", str(root)]).exit_code != 0
