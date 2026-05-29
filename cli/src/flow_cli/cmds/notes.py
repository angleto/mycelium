"""``flow note`` — add (text/voice), list, show, edit, tag, attach, archive/restore."""

from __future__ import annotations

import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import typer

from flow_cli.cmds._common import client, get_json, resolve_id, short_id
from flow_cli.completion import complete_note_id, complete_task_id
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
        autocompletion=complete_task_id,
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
def show(note_id: str = typer.Argument(..., autocompletion=complete_note_id)) -> None:
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
    note_id: str = typer.Argument(..., autocompletion=complete_note_id),
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
        autocompletion=complete_task_id,
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
def archive(note_id: str = typer.Argument(..., autocompletion=complete_note_id)) -> None:
    _action(note_id, "archive")


@app.command()
def unarchive(note_id: str = typer.Argument(..., autocompletion=complete_note_id)) -> None:
    _action(note_id, "unarchive")


@app.command("delete")
def delete_(note_id: str = typer.Argument(..., autocompletion=complete_note_id)) -> None:
    """Soft-delete a note (recoverable)."""
    _action(note_id, "delete")


@app.command()
def restore(note_id: str = typer.Argument(..., autocompletion=complete_note_id)) -> None:
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
    note_id: str = typer.Argument(..., autocompletion=complete_note_id),
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
def attach_list(note_id: str = typer.Argument(..., autocompletion=complete_note_id)) -> None:
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


# --- parts sub-group (Phase 5, task ce8aaed2) -----------------------
# Multi-part markdown notes. Mirrors the REST surface from Phase 2a/2b
# (POST /notes/{id}/parts, PATCH /notes/{id}/parts/{pid}, DELETE,
# PUT /parts/order, POST /notes/merge).

parts_app = typer.Typer(
    no_args_is_help=True,
    help="Ordered markdown parts inside a note (add/list/edit/rm/reorder).",
)
app.add_typer(parts_app, name="parts")


def _resolve_part(c: Any, note_id: str, partial: str) -> str:
    """Resolve a short-id (or full uuid) to a part id within a note."""
    if len(partial) >= 32:
        return partial
    rows = get_json(c.get(f"/notes/{note_id}/parts"))
    short = partial.lower()
    matches = [p for p in rows if str(p["id"]).lower().startswith(short)]
    if not matches:
        raise CLIError(f"no part matching '{partial}' on note {short_id(note_id)}.")
    if len(matches) > 1:
        raise CLIError(f"part prefix '{partial}' is ambiguous on note {short_id(note_id)}.")
    return str(matches[0]["id"])


@parts_app.command("add")
def parts_add(
    note_id: str = typer.Argument(..., autocompletion=complete_note_id),
    file: Path | None = typer.Option(
        None,
        "--file",
        "-f",
        help="Read body from a file. Use '-' for stdin. Omit to open $EDITOR.",
    ),
    lang: str | None = typer.Option(
        None, "--lang", "-l", help="ISO 639-1 hint (en, it, ...). Optional."
    ),
    ord: int | None = typer.Option(
        None,
        "--ord",
        help="Insert at this ord (shifts following parts forward). Omit = append.",
    ),
) -> None:
    """Add a markdown part to a note. Body comes from --file, stdin
    (-), or an interactive $EDITOR session."""
    if file is not None:
        if str(file) == "-":
            body = sys.stdin.read()
        else:
            body = file.read_text()
    else:
        body = edit_in_editor("")
    with client() as c:
        full = _resolve_note(c, note_id)
        payload: dict[str, Any] = {"body": body}
        if lang:
            payload["lang"] = lang
        if ord is not None:
            payload["ord"] = ord
        part = get_json(c.post(f"/notes/{full}/parts", json=payload))
    if json_mode():
        emit_json(part)
        return
    success(
        f"added part {short_id(part['id'])} to note {short_id(full)} "
        f"(ord={part['ord']}, {len(body)} chars)"
    )


@parts_app.command("append")
def parts_append(
    note_id: str = typer.Argument(..., autocompletion=complete_note_id),
    part_id: str | None = typer.Argument(
        None, help="Existing part to append to. Omit (or use --new) to create one."
    ),
    file: Path = typer.Option(
        ..., "--file", "-f", help="Markdown file to stream in. Use '-' for stdin."
    ),
    new: bool = typer.Option(
        False, "--new", help="Create a new part from the content instead of appending."
    ),
    title: str | None = typer.Option(None, "--title", help="Title for the new part (with --new)."),
    lang: str | None = typer.Option(None, "--lang", "-l", help="ISO 639-1 hint. Optional."),
    chunk_size: int = typer.Option(
        32768, "--chunk-size", help="Chars per chunk; keep under the transport payload cap."
    ),
) -> None:
    """Stream a (possibly large) markdown file into a note part in
    chunks, past the ~100k-char transport payload cap (task 27f4d6c9).
    Give an existing PART-ID to append to it, or --new to create the
    part on the fly. Reads from a file or stdin ('-'). Chunks reassemble
    byte-for-byte; a failed run is safe to re-run (idempotent replay)."""
    if chunk_size < 1:
        raise CLIError("--chunk-size must be >= 1.")
    text = sys.stdin.read() if str(file) == "-" else file.read_text()
    if not text:
        raise CLIError("nothing to append (empty input).")
    chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]
    op_id = uuid.uuid4().hex
    with client() as c:
        full = _resolve_note(c, note_id)
        if new or not part_id:
            payload: dict[str, Any] = {"body": chunks[0]}
            if title:
                payload["title"] = title
            if lang:
                payload["lang"] = lang
            first = get_json(c.post(f"/notes/{full}/parts", json=payload))
            pid = str(first["id"])
            version = int(first["version"])
            start = 1
            total_appended = len(chunks[0])
        else:
            pid = _resolve_part(c, full, part_id)
            rows = get_json(c.get(f"/notes/{full}/parts"))
            part = next(p for p in rows if p["id"] == pid)
            version = int(part["version"])
            start = 0
            total_appended = 0
        indices: Any = range(start, len(chunks))
        if not json_mode() and len(chunks) - start > 0:
            from rich.progress import track

            indices = track(indices, description=f"appending {len(chunks)} chunk(s)")
        for idx in indices:
            resp = get_json(
                c.post(
                    f"/notes/{full}/parts/{pid}/append",
                    json={
                        "chunk": chunks[idx],
                        "expected_version": version,
                        "chunk_index": idx,
                        "is_last": idx == len(chunks) - 1,
                        "operation_id": op_id,
                    },
                )
            )
            version = int(resp["version"])
            total_appended += int(resp["appended_chars"])
    if json_mode():
        emit_json({"part_id": pid, "version": version, "appended_chars": total_appended})
        return
    success(
        f"appended {total_appended} chars to part {short_id(pid)} "
        f"on note {short_id(full)} in {len(chunks)} chunk(s) (v{version})"
    )


@parts_app.command("prepend")
def parts_prepend(
    note_id: str = typer.Argument(..., autocompletion=complete_note_id),
    part_id: str = typer.Argument(...),
    text: str | None = typer.Option(
        None,
        "--text",
        "-m",
        help="Text to prepend. Use '-' for stdin; omit to open $EDITOR.",
    ),
) -> None:
    """Prepend markdown text to the FRONT of a part without resending the
    body (task 5662a07f). Concatenated raw, so include any trailing
    newline yourself (e.g. a heading: '# Title\\n\\n'). Concurrency-safe."""
    if text == "-":
        text = sys.stdin.read()
    elif text is None:
        text = edit_in_editor("")
    if not text:
        raise CLIError("nothing to prepend (empty text).")
    with client() as c:
        full = _resolve_note(c, note_id)
        pid = _resolve_part(c, full, part_id)
        rows = get_json(c.get(f"/notes/{full}/parts"))
        part = next(p for p in rows if p["id"] == pid)
        resp = get_json(
            c.post(
                f"/notes/{full}/parts/{pid}/prepend",
                json={"text": text, "expected_version": part["version"]},
            )
        )
    if json_mode():
        emit_json(resp)
        return
    success(
        f"prepended {resp.get('appended_chars')} chars to part {short_id(pid)} "
        f"on note {short_id(full)} (v{resp.get('version')})"
    )


@parts_app.command("replace")
def parts_replace(
    note_id: str = typer.Argument(..., autocompletion=complete_note_id),
    part_id: str = typer.Argument(...),
    find: str = typer.Argument(..., help="Literal text to find."),
    replace: str = typer.Argument(..., help="Replacement text."),
    count: int = typer.Option(
        0, "--count", "-c", min=0, help="Max replacements; 0 (default) = all occurrences."
    ),
) -> None:
    """Find-and-replace literal text inside one part without resending the
    body (task 5662a07f). ``--count 0`` (default) replaces every
    occurrence; a positive count only the first N. Concurrency-safe; a
    no-op (text not found) reports 0 replacements without bumping."""
    with client() as c:
        full = _resolve_note(c, note_id)
        pid = _resolve_part(c, full, part_id)
        rows = get_json(c.get(f"/notes/{full}/parts"))
        part = next(p for p in rows if p["id"] == pid)
        resp = get_json(
            c.post(
                f"/notes/{full}/parts/{pid}/replace",
                json={
                    "find": find,
                    "replace": replace,
                    "expected_version": part["version"],
                    "count": count,
                },
            )
        )
    if json_mode():
        emit_json(resp)
        return
    n = resp.get("replacements", 0)
    if n == 0:
        info(f"'{find}' not found in part {short_id(pid)}; no change.")
        return
    success(
        f"replaced {n} occurrence(s) in part {short_id(pid)} "
        f"on note {short_id(full)} (v{resp.get('version')})"
    )


@parts_app.command("list")
def parts_list(
    note_id: str = typer.Argument(..., autocompletion=complete_note_id),
) -> None:
    """List the ordered parts of a note (short id, ord, lang, preview)."""
    with client() as c:
        full = _resolve_note(c, note_id)
        rows = get_json(c.get(f"/notes/{full}/parts"))
    if json_mode():
        emit_json(rows)
        return
    emit_table(
        None,
        ["id", "ord", "lang", "chars", "preview"],
        [
            (
                short_id(r["id"]),
                r["ord"],
                r.get("lang") or "",
                len(r.get("body") or ""),
                _truncate(
                    (r.get("body") or "").strip().splitlines()[0]
                    if (r.get("body") or "").strip()
                    else "",
                    60,
                ),
            )
            for r in rows
        ],
    )


@parts_app.command("edit")
def parts_edit(
    note_id: str = typer.Argument(..., autocompletion=complete_note_id),
    part_id: str = typer.Argument(...),
) -> None:
    """Open the part body in $EDITOR; save to PATCH back."""
    with client() as c:
        full = _resolve_note(c, note_id)
        pid = _resolve_part(c, full, part_id)
        current = get_json(c.get(f"/notes/{full}/parts"))
        part = next(p for p in current if p["id"] == pid)
        new_body = edit_in_editor(part.get("body") or "")
        if new_body == (part.get("body") or ""):
            info("no changes; skip")
            return
        resp = get_json(
            c.patch(
                f"/notes/{full}/parts/{pid}",
                json={"expected_version": part["version"], "body": new_body},
            )
        )
    success(f"updated part {short_id(pid)} (v{resp.get('version')})")


@parts_app.command("rm")
def parts_rm(
    note_id: str = typer.Argument(..., autocompletion=complete_note_id),
    part_id: str = typer.Argument(...),
) -> None:
    """Hard-delete a part. Remaining ords stay as-is (no compaction)."""
    with client() as c:
        full = _resolve_note(c, note_id)
        pid = _resolve_part(c, full, part_id)
        resp = c.delete(f"/notes/{full}/parts/{pid}")
        if resp.status_code not in (200, 204):
            get_json(resp)
    success(f"deleted part {short_id(pid)} from note {short_id(full)}")


@parts_app.command("reorder")
def parts_reorder(
    note_id: str = typer.Argument(..., autocompletion=complete_note_id),
    part_ids: list[str] = typer.Argument(
        ..., help="Full set of part ids in the desired order (short ids accepted)."
    ),
) -> None:
    """Rewrite the entire part ordering. Must list EVERY part (the
    server refuses a partial set so a typo can't silently drop a row)."""
    with client() as c:
        full = _resolve_note(c, note_id)
        resolved = [_resolve_part(c, full, p) for p in part_ids]
        rows = get_json(c.put(f"/notes/{full}/parts/order", json={"part_ids": resolved}))
    if json_mode():
        emit_json(rows)
        return
    success(
        f"reordered {len(rows)} parts on note {short_id(full)}: "
        + " → ".join(short_id(r["id"]) for r in rows)
    )


@app.command("merge")
def merge(
    source: str = typer.Argument(..., autocompletion=complete_note_id),
    target: str = typer.Argument(..., autocompletion=complete_note_id),
    strategy: str = typer.Option(
        "append",
        "--strategy",
        "-s",
        help="Merge strategy. v1 ships 'append' only.",
    ),
) -> None:
    """Fold source's parts into target. Source is soft-deleted; target
    supersedes it (visible as a 'supersedes' link). Idempotent."""
    with client() as c:
        src_id = _resolve_note(c, source)
        tgt_id = _resolve_note(c, target)
        out_note = get_json(
            c.post(
                "/notes/merge",
                json={
                    "source_note_id": src_id,
                    "target_note_id": tgt_id,
                    "strategy": strategy,
                },
            )
        )
    if json_mode():
        emit_json(out_note)
        return
    success(
        f"merged note {short_id(src_id)} → {short_id(tgt_id)} "
        f"({len(out_note.get('parts', []))} parts now on target)"
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
    task: str | None = typer.Option(
        None,
        "--task",
        autocompletion=complete_task_id,
        help="Link to this task.",
    ),
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
