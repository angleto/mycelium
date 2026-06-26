"""``mycelium annotate`` — inline comments and suggestions on markdown
documents (note parts, task descriptions).

This is the CLI face of the annotation layer the web editor renders
inline. A terminal cannot select a range, so a document is addressed by
the generic ``<doc_kind> <doc_id>`` handle (``note_part`` + part id, or
``task_description`` + task id) and a passage by its ``--quote`` /
``--original`` text. Proposing/accepting/listing works here exactly as
on the web and over MCP; only the inline highlight is web-only.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import typer

from mycelium_cli.cmds._common import client, get_json, short_id
from mycelium_cli.http import CLIError
from mycelium_cli.ui import edit_in_editor, emit_json, info, json_mode, success

app = typer.Typer(
    no_args_is_help=True,
    help="Inline comments & suggestions on note parts / task descriptions.",
)

_DOC_HELP = "Document kind: 'note_part' or 'task_description'."


def _annotation_version(c: object, annotation_id: str) -> int:
    return int(get_json(c.get(f"/annotations/{annotation_id}"))["version"])  # type: ignore[attr-defined]


def _print_row(a: dict[str, Any]) -> None:
    if json_mode():
        emit_json(a)
        return
    label = a.get("body") or a.get("anchor_quote") or ""
    success(f"{a['kind']} {short_id(a['id'])} [{a['status']}] {label[:60]}")


@app.command("list")
def list_annotations(
    doc_kind: str = typer.Argument(..., help=_DOC_HELP),
    doc_id: str = typer.Argument(..., help="note_part id or task id."),
    include_resolved: bool = typer.Option(True, "--resolved/--open-only"),
) -> None:
    """List the comments and suggestions on a document (oldest first; a
    task's general comments are its work diary)."""
    with client() as c:
        rows = get_json(
            c.get(
                "/annotations",
                params={
                    "doc_kind": doc_kind,
                    "doc_id": doc_id,
                    "include_resolved": include_resolved,
                },
            )
        )
    if json_mode():
        emit_json(rows)
        return
    if not rows:
        info("[dim]no annotations.[/dim]")
        return
    for r in rows:
        label = (r.get("body") or r.get("anchor_quote") or "")[:60]
        anchor = "·" if r.get("anchor_quote") else " "
        out_line = f"  [{short_id(r['id'])}] {r['kind']:<10} {r['status']:<8} {anchor} {label}"
        info(out_line)


@app.command("comment")
def comment(
    doc_kind: str = typer.Argument(..., help=_DOC_HELP),
    doc_id: str = typer.Argument(...),
    body: str | None = typer.Option(
        None, "--body", "-m", help="Comment body. Use '-' for stdin; omit to open $EDITOR."
    ),
    body_file: Path | None = typer.Option(
        None, "--body-file", help="Read the comment body from a file (wins over --body)."
    ),
    quote: str | None = typer.Option(
        None, "--quote", help="Passage to anchor to (omit for a whole-document comment)."
    ),
    parent_id: str | None = typer.Option(None, "--reply-to", help="Reply to this annotation id."),
) -> None:
    """Add an inline comment to a document."""
    if body_file is not None:
        body = body_file.read_text()
    elif body == "-":
        body = sys.stdin.read().strip()
    elif body is None:
        body = edit_in_editor("").strip()
    if not body:
        raise CLIError("empty comment body, aborting.")
    with client() as c:
        a = get_json(
            c.post(
                "/annotations/comment",
                json={
                    "doc_kind": doc_kind,
                    "doc_id": doc_id,
                    "body": body,
                    "anchor_quote": quote,
                    "parent_id": parent_id,
                },
            )
        )
    _print_row(a)


@app.command("suggest")
def suggest(
    doc_kind: str = typer.Argument(..., help=_DOC_HELP),
    doc_id: str = typer.Argument(...),
    original: str | None = typer.Option(None, "--original", "-o", help="Text to replace."),
    original_file: Path | None = typer.Option(
        None, "--original-file", help="Read --original from a file (wins over --original)."
    ),
    proposed: str | None = typer.Option(
        None, "--proposed", "-p", help="Replacement (empty = deletion)."
    ),
    proposed_file: Path | None = typer.Option(
        None, "--proposed-file", help="Read --proposed from a file (wins over --proposed)."
    ),
    why: str = typer.Option("", "--why", help="Rationale."),
) -> None:
    """Propose an edit (original -> proposed). Nothing changes in the
    document until the suggestion is accepted."""
    if original_file is not None:
        original = original_file.read_text()
    if proposed_file is not None:
        proposed = proposed_file.read_text()
    if not original:
        raise CLIError("provide --original or --original-file (the text to replace).")
    if proposed is None:
        raise CLIError("provide --proposed or --proposed-file (use --proposed '' to delete).")
    with client() as c:
        a = get_json(
            c.post(
                "/annotations/suggestion",
                json={
                    "doc_kind": doc_kind,
                    "doc_id": doc_id,
                    "original_text": original,
                    "proposed_text": proposed,
                    "rationale": why,
                },
            )
        )
    _print_row(a)


def _act(annotation_id: str, action: str) -> None:
    with client() as c:
        version = _annotation_version(c, annotation_id)
        get_json(
            c.post(f"/annotations/{annotation_id}/{action}", json={"expected_version": version})
        )
    success(f"{action} {short_id(annotation_id)}")


@app.command("resolve")
def resolve(annotation_id: str = typer.Argument(...)) -> None:
    """Mark a comment thread resolved."""
    _act(annotation_id, "resolve")


@app.command("reopen")
def reopen(annotation_id: str = typer.Argument(...)) -> None:
    """Reopen a resolved comment thread."""
    _act(annotation_id, "reopen")


@app.command("accept")
def accept(annotation_id: str = typer.Argument(...)) -> None:
    """Accept a suggestion: splice it into the document body."""
    _act(annotation_id, "accept")


@app.command("reject")
def reject(annotation_id: str = typer.Argument(...)) -> None:
    """Reject a pending suggestion (document untouched)."""
    _act(annotation_id, "reject")


@app.command("edit")
def edit(
    annotation_id: str = typer.Argument(...),
    body: str | None = typer.Option(None, "--body", "-m", help="New body."),
    body_file: Path | None = typer.Option(
        None, "--body-file", help="Read the new body from a file (wins over --body)."
    ),
) -> None:
    """Edit an annotation body (author or admin only)."""
    if body_file is not None:
        body = body_file.read_text()
    if not body:
        raise CLIError("provide --body or --body-file.")
    with client() as c:
        version = _annotation_version(c, annotation_id)
        get_json(
            c.patch(
                f"/annotations/{annotation_id}",
                json={"body": body, "expected_version": version},
            )
        )
    success(f"edited {short_id(annotation_id)}")


@app.command("rm")
def remove(annotation_id: str = typer.Argument(...)) -> None:
    """Soft-delete an annotation / withdraw a pending suggestion."""
    with client() as c:
        version = _annotation_version(c, annotation_id)
        resp = c.delete(f"/annotations/{annotation_id}", params={"expected_version": version})
        if resp.status_code not in (200, 204):
            get_json(resp)
    success(f"removed {short_id(annotation_id)}")
