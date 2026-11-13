"""The note's MCP tools, bound to the operation registry (ADR-0068).

Imported at the end of ``server.py``, like the garden's. Reading a note
answers lean: null, false and empty fields are left out, and a part does
not repeat the id of the note it belongs to.
"""

from __future__ import annotations

from typing import Any

from mycelium_mcp.op_context import op_ctx
from mycelium_mcp.op_tools import bind_tool
from mycelium_mcp.server import mcp


def _without_note_id(part: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in part.items() if k != "note_id"}


def _note(data: dict[str, Any]) -> dict[str, Any]:
    """The outline travels as ``parts``, the key this tool has always used
    for it, and each part drops the note id the caller already has."""
    outline = data.pop("outline", None)
    if outline is not None:
        data["parts"] = outline
    data["parts"] = [_without_note_id(p) for p in data.get("parts") or []]
    return data


def _parts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_without_note_id(p) for p in rows]


def _page(data: dict[str, Any], fields: list[str] | None = None) -> dict[str, Any]:
    """Keep only the named columns of each row (``id`` always), for a
    picker that needs ``[id, title]`` and not the whole row."""
    if fields is not None:
        keep = set(fields) | {"id"}
        data["items"] = [{k: v for k, v in row.items() if k in keep} for row in data["items"]]
    return data


TOOLS: dict[str, Any] = {
    "list_notes": bind_tool(
        mcp,
        op_ctx,
        "notes.list",
        name="list_notes",
        present=_page,
        lean_answer=True,
        # The paged-list contract every list_* tool keeps: the envelope is
        # whole even on the last page.
        lean_keep=frozenset({"items", "next_cursor", "truncated"}),
        extras={
            "fields": (
                list[str] | None,
                None,
                "Keep only these columns of each row (id is always kept).",
            )
        },
    ),
    # A reader of ``parts`` does not need the joined body as well.
    "get_note": bind_tool(
        mcp,
        op_ctx,
        "notes.get",
        name="get_note",
        present=_note,
        defaults={"include_transcript": False},
        lean_answer=True,
    ),
    # The outline is what this tool is for; the bodies are one flag away.
    "list_note_parts": bind_tool(
        mcp,
        op_ctx,
        "notes.parts.list",
        name="list_note_parts",
        present=_parts,
        defaults={"include_body": False},
        lean_answer=True,
    ),
}
