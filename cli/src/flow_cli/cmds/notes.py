"""``flow note`` — add (text or voice), list, show."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import typer

from flow_cli.cmds._common import client, short_id
from flow_cli.http import CLIError, raise_for_response
from flow_cli.ui import edit_in_editor, emit_json, emit_table, info, json_mode, out, success, warn

app = typer.Typer(no_args_is_help=True, help="Notes: capture text or voice memos.")


@app.command()
def add(
    title: str | None = typer.Option(None, "--title", "-t", help="Optional title."),
    text: str | None = typer.Option(
        None,
        "--text",
        "-m",
        help="Note body. Use '-' to read from stdin; omit to open $EDITOR.",
    ),
    no_editor: bool = typer.Option(False, "--no-editor"),
    kind: str = typer.Option("text", "--kind", help="Note kind: text | voice | conversation."),
) -> None:
    """Create a text note. With no -m/--text, opens $EDITOR."""
    if text == "-":
        import sys as _sys

        text = _sys.stdin.read().strip() or None
    elif text is None and not no_editor:
        text = edit_in_editor("").strip() or None
    if not text and kind == "text":
        raise CLIError(
            "Empty note body, aborting.",
            hint="Pass --text or write something in $EDITOR.",
        )
    payload: dict[str, Any] = {"kind": kind}
    if title:
        payload["title"] = title
    if text:
        payload["text"] = text
    with client() as c:
        created = _get_json(c.post("/notes", json=payload))
    if json_mode():
        emit_json(created)
        return
    success(f"Created note [bold]{short_id(created['id'])}[/bold]")


@app.command("list")
def list_(
    limit: int = typer.Option(30, "--limit", "-n", min=1, max=500),
    archived: bool = typer.Option(False, "--archived/--no-archived"),
) -> None:
    """List recent notes."""
    with client() as c:
        rows = _get_json(c.get("/notes", params={"include_archived": str(archived).lower()}))
    rows = rows[:limit]
    if json_mode():
        emit_json(rows)
        return
    emit_table(
        None,
        ["id", "kind", "title", "tags", "task"],
        [
            (
                short_id(r.get("id")),
                r.get("kind"),
                _truncate(r.get("title") or (r.get("transcript") or "")[:80], 60),
                ",".join(t.get("name", "") for t in r.get("tags") or []),
                short_id(r.get("task_id")),
            )
            for r in rows
        ],
    )


@app.command()
def show(note_id: str = typer.Argument(...)) -> None:
    """Print a note's title and full body."""
    with client() as c:
        full = _resolve_note(c, note_id)
        note = _get_json(c.get(f"/notes/{full}"))
    if json_mode():
        emit_json(note)
        return
    out().print(f"[bold]{note.get('title') or '<untitled>'}[/bold]  [dim]({note['kind']})[/dim]")
    out().print(f"id: {note['id']}  task: {note.get('task_id') or '-'}  ver: {note['version']}")
    body = note.get("transcript")
    if body:
        out().print("")
        out().print(body)


@app.command()
def voice(
    seconds: int | None = typer.Option(
        None,
        "--seconds",
        "-s",
        help="Auto-stop after N seconds (default: record until Ctrl-C).",
    ),
    title: str | None = typer.Option(None, "--title", "-t"),
    keep_audio: bool = typer.Option(
        False, "--keep-audio", help="Keep the local recording after upload."
    ),
) -> None:
    """Record an audio memo via sox/ffmpeg and upload it as a voice note."""
    recorder = _find_recorder()
    if recorder is None:
        raise CLIError(
            "Neither 'sox' (rec) nor 'ffmpeg' is on PATH.",
            hint="brew install sox  # or  brew install ffmpeg",
        )
    out_path = Path(tempfile.mkstemp(suffix=".wav", prefix="flow-voice-")[1])
    cmd = _build_record_cmd(recorder, out_path, seconds=seconds)
    info(f"[dim]recording → {out_path}  (Ctrl-C to stop)[/dim]")
    t0 = time.monotonic()
    try:
        subprocess.run(cmd, check=False)  # noqa: S603
    except KeyboardInterrupt:
        pass
    elapsed = max(1, int(time.monotonic() - t0))
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise CLIError("Recording produced no audio; aborting.")
    info(f"[dim]recorded {elapsed}s, uploading…[/dim]")

    with client() as c:
        # Create the note shell first (kind=voice). The audio is then
        # attached via /notes/{id}/attachments, mirroring the SPA's
        # capture flow which separates the row from the binary upload.
        payload: dict[str, Any] = {"kind": "voice", "audio_seconds": elapsed}
        if title:
            payload["title"] = title
        note = _get_json(c.post("/notes", json=payload))
        note_id = str(note["id"])
        with out_path.open("rb") as fh:
            files = {"file": (out_path.name, fh, "audio/wav")}
            resp = c.post(f"/notes/{note_id}/attachments", files=files)
            raise_for_response(resp)
    if not keep_audio:
        try:
            os.unlink(out_path)
        except OSError:
            warn(f"could not delete temp file {out_path}")
    if json_mode():
        emit_json({"id": note_id, "audio_seconds": elapsed})
        return
    success(f"Captured voice note [bold]{short_id(note_id)}[/bold] ({elapsed}s)")


def _find_recorder() -> str | None:
    if shutil.which("rec"):
        return "rec"
    if shutil.which("sox"):
        return "sox"
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    return None


def _build_record_cmd(recorder: str, out_path: Path, *, seconds: int | None) -> list[str]:
    if recorder in ("rec", "sox"):
        # sox/rec: mono, 16 kHz, signed 16-bit — small and friendly to
        # speech-to-text. ``-q`` suppresses the verbose meter that fights
        # the user's Rich output.
        base = [recorder, "-q", "-c", "1", "-r", "16000", str(out_path)]
        if seconds:
            base += ["trim", "0", str(seconds)]
        return base
    # ffmpeg input device varies per OS; ``avfoundation`` is the macOS
    # default, ``alsa`` for Linux. The user can override via env var if
    # this guess is wrong.
    if "FLOW_FFMPEG_INPUT" in os.environ:
        spec = os.environ["FLOW_FFMPEG_INPUT"].split(",")
    elif _is_macos():
        spec = ["-f", "avfoundation", "-i", ":0"]
    else:
        spec = ["-f", "alsa", "-i", "default"]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *spec]
    if seconds:
        cmd += ["-t", str(seconds)]
    cmd += ["-ac", "1", "-ar", "16000", str(out_path)]
    return cmd


def _is_macos() -> bool:
    import sys

    return sys.platform == "darwin"


def _resolve_note(c: Any, partial: str) -> str:
    if len(partial) >= 32:
        return partial
    rows = _get_json(c.get("/notes", params={"include_archived": "true"}))
    matches = [n for n in rows if str(n.get("id", "")).startswith(partial)]
    if len(matches) == 1:
        return str(matches[0]["id"])
    if not matches:
        raise CLIError(f"No note matches '{partial}'.")
    raise CLIError(f"Ambiguous note prefix '{partial}' ({len(matches)} matches).")


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _get_json(resp: Any) -> Any:
    raise_for_response(resp)
    return resp.json()
