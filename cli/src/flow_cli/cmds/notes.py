"""``flow note`` — add (text/voice), list, show, edit, tag, attach, archive/restore."""

from __future__ import annotations

import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import typer

from flow_cli.cmds._common import client, get_json, resolve_id, short_id
from flow_cli.http import CLIError, raise_for_response
from flow_cli.ui import (
    edit_in_editor,
    emit_json,
    emit_table,
    info,
    json_mode,
    out,
    success,
    warn,
)

app = typer.Typer(no_args_is_help=True, help="Notes: capture text or voice memos.")


def _resolve_note(c: Any, partial: str) -> str:
    return resolve_id(c, partial, endpoint="/notes", kind="note")


def _resolve_tag(c: Any, name_or_id: str) -> str:
    if len(name_or_id) >= 32:
        return name_or_id
    rows = get_json(c.get("/tags"))
    matches = [t for t in rows if str(t.get("name")).lower() == name_or_id.lower()]
    if not matches:
        raise CLIError(f"no tag named '{name_or_id}'.")
    return str(matches[0]["id"])


def _resolve_task(c: Any, partial: str) -> str:
    return resolve_id(c, partial, endpoint="/tasks", kind="task")


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
    task: str | None = typer.Option(
        None,
        "--task",
        help="Link the note to this task (Proposal A: note → task pre-bind).",
    ),
    tag: list[str] = typer.Option([], "--tag", help="Tag name or UUID; pass multiple times."),
) -> None:
    """Create a text note. With no -m/--text, opens $EDITOR."""
    if text == "-":
        text = sys.stdin.read().strip() or None
    elif text is None and not no_editor:
        text = edit_in_editor("").strip() or None
    if not text:
        raise CLIError(
            "empty note body, aborting.",
            hint="Pass --text or write something in $EDITOR.",
        )
    payload: dict[str, Any] = {"kind": "text"}
    if title:
        payload["title"] = title
    payload["text"] = text
    with client() as c:
        created = get_json(c.post("/notes", json=payload))
        note_id = str(created["id"])
        # Optional task link is a follow-up PATCH so we keep one write
        # per concept (no atomicity needed, the note is already saved).
        if task:
            full_task = _resolve_task(c, task)
            patch = {
                "expected_version": created["version"],
                "task_id": full_task,
            }
            get_json(c.patch(f"/notes/{note_id}", json=patch))
        for t in tag:
            tag_id = _resolve_tag(c, t)
            resp = c.post(f"/notes/{note_id}/tags", json={"tag_id": tag_id})
            if resp.status_code not in (200, 204):
                get_json(resp)
    if json_mode():
        emit_json(created)
        return
    success(f"created note [bold]{short_id(note_id)}[/bold]")


@app.command("list")
def list_(
    limit: int = typer.Option(30, "--limit", "-n", min=1, max=500),
    archived: bool = typer.Option(False, "--archived/--no-archived"),
    tag: str | None = typer.Option(None, "--tag", help="Filter by tag name or UUID."),
) -> None:
    """List recent notes."""
    params: dict[str, str] = {"include_archived": str(archived).lower()}
    with client() as c:
        if tag:
            params["tag_id"] = _resolve_tag(c, tag)
        rows = get_json(c.get("/notes", params=params))
    rows = rows[:limit]
    if json_mode():
        emit_json(rows)
        return
    if not rows:
        info("[dim]no notes.[/dim]")
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
        note = get_json(c.get(f"/notes/{full}"))
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
def edit(
    note_id: str = typer.Argument(...),
    title: str | None = typer.Option(None, "--title"),
    text: str | None = typer.Option(
        None,
        "--text",
        "-m",
        help="New body. Use '-' for stdin; '@' to open $EDITOR pre-loaded with the current text.",
    ),
    task: str | None = typer.Option(
        None,
        "--task",
        help="Set the note→task link. Use '-' to unlink.",
    ),
) -> None:
    """Patch title/text/task-link on an existing note."""
    with client() as c:
        full = _resolve_note(c, note_id)
        current = get_json(c.get(f"/notes/{full}"))
        payload: dict[str, Any] = {"expected_version": current["version"]}
        if title is not None:
            payload["title"] = title
        if text == "-":
            payload["text"] = sys.stdin.read()
        elif text == "@":
            payload["text"] = edit_in_editor(current.get("transcript") or "")
        elif text is not None:
            payload["text"] = text
        if task == "-":
            payload["task_id"] = None
        elif task is not None:
            payload["task_id"] = _resolve_task(c, task)
        result = get_json(c.patch(f"/notes/{full}", json=payload))
    if json_mode():
        emit_json(result)
        return
    success(f"updated note {short_id(full)} (v{result.get('version')})")


@app.command()
def archive(note_id: str = typer.Argument(...)) -> None:
    _action(note_id, "archive")


@app.command()
def unarchive(note_id: str = typer.Argument(...)) -> None:
    _action(note_id, "unarchive")


@app.command("delete")
def delete_(note_id: str = typer.Argument(...)) -> None:
    """Soft-delete a note (recoverable)."""
    _action(note_id, "delete")


@app.command()
def restore(note_id: str = typer.Argument(...)) -> None:
    _action(note_id, "restore")


# --- tag / attach sub-groups -----------------------------------------

tag_app = typer.Typer(no_args_is_help=True, help="Add/remove tags on a note.")
app.add_typer(tag_app, name="tag")

attach_app = typer.Typer(no_args_is_help=True, help="Attachments on a note.")
app.add_typer(attach_app, name="attach")


@tag_app.command("add")
def tag_add(note_id: str = typer.Argument(...), tag: str = typer.Argument(...)) -> None:
    with client() as c:
        full = _resolve_note(c, note_id)
        tag_id = _resolve_tag(c, tag)
        resp = c.post(f"/notes/{full}/tags", json={"tag_id": tag_id})
        if resp.status_code not in (200, 204):
            get_json(resp)
    success(f"tagged note {short_id(full)} with '{tag}'")


@tag_app.command("rm")
def tag_rm(note_id: str = typer.Argument(...), tag: str = typer.Argument(...)) -> None:
    with client() as c:
        full = _resolve_note(c, note_id)
        tag_id = _resolve_tag(c, tag)
        resp = c.delete(f"/notes/{full}/tags/{tag_id}")
        if resp.status_code not in (200, 204):
            get_json(resp)
    success(f"detached '{tag}' from note {short_id(full)}")


@attach_app.command("add")
def attach_add(
    note_id: str = typer.Argument(...),
    path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
) -> None:
    """Upload a file (image, PDF, ...) as an attachment on a note."""
    with client() as c:
        full = _resolve_note(c, note_id)
        with path.open("rb") as fh:
            files = {"file": (path.name, fh, _guess_mime(path))}
            res = get_json(c.post(f"/notes/{full}/attachments", files=files))
    if json_mode():
        emit_json(res)
        return
    success(f"uploaded '{path.name}' to note {short_id(full)}")


@attach_app.command("list")
def attach_list(note_id: str = typer.Argument(...)) -> None:
    with client() as c:
        full = _resolve_note(c, note_id)
        rows = get_json(c.get(f"/notes/{full}/attachments"))
    if json_mode():
        emit_json(rows)
        return
    emit_table(
        None,
        ["id", "filename", "size", "mime"],
        [
            (
                short_id(r.get("id")),
                r.get("filename"),
                r.get("size_bytes"),
                r.get("mime_type"),
            )
            for r in rows
        ],
    )


# --- voice ----------------------------------------------------------


@app.command()
def voice(
    seconds: int | None = typer.Option(
        None,
        "--seconds",
        "-s",
        help="Auto-stop after N seconds (default: record until Ctrl-C).",
    ),
    title: str | None = typer.Option(None, "--title", "-t"),
    task: str | None = typer.Option(None, "--task", help="Link to this task."),
    keep_audio: bool = typer.Option(
        False, "--keep-audio", help="Keep the local recording after upload."
    ),
) -> None:
    """Record an audio memo via sox/ffmpeg and upload it as a voice note."""
    recorder = _find_recorder()
    if recorder is None:
        raise CLIError(
            "neither 'sox' (rec) nor 'ffmpeg' is on PATH.",
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
        raise CLIError("recording produced no audio; aborting.")
    info(f"[dim]recorded {elapsed}s, uploading…[/dim]")

    with client() as c:
        payload: dict[str, Any] = {"kind": "voice", "audio_seconds": elapsed}
        if title:
            payload["title"] = title
        note = get_json(c.post("/notes", json=payload))
        note_id = str(note["id"])
        with out_path.open("rb") as fh:
            files = {"file": (out_path.name, fh, "audio/wav")}
            resp = c.post(f"/notes/{note_id}/attachments", files=files)
            raise_for_response(resp)
        if task:
            full_task = _resolve_task(c, task)
            patch = {"expected_version": note["version"], "task_id": full_task}
            get_json(c.patch(f"/notes/{note_id}", json=patch))
    if not keep_audio:
        try:
            os.unlink(out_path)
        except OSError:
            warn(f"could not delete temp file {out_path}")
    if json_mode():
        emit_json({"id": note_id, "audio_seconds": elapsed})
        return
    success(f"captured voice note [bold]{short_id(note_id)}[/bold] ({elapsed}s)")


# --- internals ------------------------------------------------------


def _action(note_id: str, action: str) -> None:
    with client() as c:
        full = _resolve_note(c, note_id)
        current = get_json(c.get(f"/notes/{full}"))
        get_json(
            c.post(
                f"/notes/{full}/{action}",
                json={"expected_version": current["version"]},
            )
        )
    success(f"note {short_id(full)} {action}d")


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
        base = [recorder, "-q", "-c", "1", "-r", "16000", str(out_path)]
        if seconds:
            base += ["trim", "0", str(seconds)]
        return base
    if "FLOW_FFMPEG_INPUT" in os.environ:
        spec = os.environ["FLOW_FFMPEG_INPUT"].split(",")
    elif sys.platform == "darwin":
        spec = ["-f", "avfoundation", "-i", ":0"]
    else:
        spec = ["-f", "alsa", "-i", "default"]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *spec]
    if seconds:
        cmd += ["-t", str(seconds)]
    cmd += ["-ac", "1", "-ar", "16000", str(out_path)]
    return cmd


def _guess_mime(path: Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"
