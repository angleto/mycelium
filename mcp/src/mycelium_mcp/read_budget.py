"""The byte budget of one MCP answer that carries note text.

A note part may be up to 1 MiB, and a client refuses a tool result long
before that: on 2026-11-25 notes of 95 to 137 kB could not be read at all,
whole or by part, because each was a single part. So every note text an
answer carries is spent from one budget (``text_window.READ_BUDGET_BYTES``),
in the order the answer lists them, and a text the budget does not hold
whole says so:

- a part gets ``body_truncated``, the whole ``body_bytes`` and a
  ``next_cursor``; ``get_note_part`` with that cursor returns the next
  window, and the windows joined in order are the body exactly;
- a ``transcript`` (the joined body, which a reader asks for on top of the
  parts or instead of them) gets ``transcript_truncated`` and its whole
  ``transcript_bytes``; it has no cursor of its own, since the parts reach
  every byte of it. A ``summary`` is short in practice but has no ceiling
  (it can be appended to), so it is spent first, the same way.

The cut is presentation, not data: the operations still return whole
bodies, and the web, which renders them, reads them whole.
"""

from __future__ import annotations

from typing import Any

from mycelium_core import text_window
from mycelium_core.services.text_patch import body_sha256


class Budget:
    """What is left of one answer's budget, spent text by text."""

    def __init__(self, total: int = text_window.READ_BUDGET_BYTES) -> None:
        self.left = total

    def part(self, row: dict[str, Any], *, start: int = 0) -> dict[str, Any]:
        """Window ``row["body"]`` in place, from ``start``. A row with no
        body (an outline row) is returned as it is."""
        body = row.get("body")
        if body is None:
            return row
        w = text_window.window(body, start=start, budget=self.left)
        self.left -= w.bytes
        if w.truncated:
            row["body"] = w.text
            row["body_truncated"] = True
            row["body_bytes"] = w.total_bytes
            row["next_cursor"] = text_window.cursor(w, row.get("body_sha256") or body_sha256(body))
        return row

    def text(self, row: dict[str, Any], key: str) -> dict[str, Any]:
        """Window the text under ``key`` in place, from its start."""
        text = row.get(key)
        if not text:
            return row
        w = text_window.window(text, budget=self.left)
        self.left -= w.bytes
        if w.truncated:
            row[key] = w.text
            row[f"{key}_truncated"] = True
            row[f"{key}_bytes"] = w.total_bytes
        return row


def note(data: dict[str, Any]) -> dict[str, Any]:
    """A note answer: its summary, its parts in order, then its transcript."""
    budget = Budget()
    budget.text(data, "summary")
    for p in data.get("parts") or []:
        budget.part(p)
    return budget.text(data, "transcript")


def parts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    budget = Budget()
    return [budget.part(r) for r in rows]


def note_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A page of notes: each row's summary and, when asked, transcript."""
    budget = Budget()
    return [budget.text(budget.text(r, "summary"), "transcript") for r in rows]


__all__ = ["Budget", "note", "note_rows", "parts"]
