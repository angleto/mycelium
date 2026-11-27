"""A window on a long text, for a reader that cannot take it whole.

An MCP client refuses a tool result past a size of its own, and a note part
may be up to ``note_body_max_bytes`` (1 MiB). A cap with no way past it
would be data loss, and a way past it that hands out a download link puts a
credential in the conversation. So a long text is read by windows, each one
within a byte budget, each one saying where the next starts.

Three properties are the contract, and they are why this is one module
rather than a slice at every call site:

- **Bytes, not characters.** The limit the client applies is on what the
  wire carries. A budget in characters is four times larger in bytes on
  text that is all four-byte characters, which is the case the budget is
  for.
- **A window ends at the end of a line** when the window holds one, so a
  reader never gets half a line of markdown. A single line longer than the
  budget is cut on a character boundary, never inside a UTF-8 sequence.
  Joining the windows in order gives back the text exactly.
- **A cursor is bound to the text it was cut from.** It carries the offset
  and a prefix of the text's SHA-256; asked of a text that has changed
  since, it is refused as a stale read with the current version, so two
  revisions are never spliced into one reading.

What it does not do. It cuts on characters, not on grapheme clusters, so a
combining sequence that straddles the budget lands on two windows; joining
them restores it. And it counts the text's UTF-8 bytes, not its JSON: on the
wire a newline or a quote takes two bytes and a control character six, so a
text made of nothing else is up to six times its budget. Markdown is a few
percent newlines, and the budget is a quarter of what a client was seen
refusing, which is the margin that absorbs it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from mycelium_core.concurrency import raise_stale_version
from mycelium_core.errors import UnprocessableError
from mycelium_core.i18n import MessageCode
from mycelium_core.operations.paging import decode_cursor, encode_cursor
from mycelium_core.services.text_patch import body_sha256

#: What one MCP read may carry of free text, in UTF-8 bytes, all of its texts
#: together. Below the ~95 kB a client was seen refusing (field report,
#: 2026-11-25) by a wide margin, and equal to what ``garden_review_read``
#: already gave its sources: a result stays in the caller's context for the
#: rest of the session (ADR-0064), so the margin is not waste.
READ_BUDGET_BYTES = 24_000

#: How much of the digest a cursor carries: enough that an edit is never
#: mistaken for the same text, short enough to keep the cursor small.
_DIGEST_CHARS = 16


@dataclass(frozen=True)
class Window:
    """``text`` is ``whole[start:end]``; ``truncated`` when it is not all
    of it, and then ``next_start`` is where the next window begins (None
    once the window reaches the end)."""

    text: str
    start: int
    end: int
    total_bytes: int
    total_chars: int

    @property
    def truncated(self) -> bool:
        return self.start > 0 or self.end < self.total_chars

    @property
    def next_start(self) -> int | None:
        return self.end if self.end < self.total_chars else None

    @property
    def bytes(self) -> int:
        return len(self.text.encode("utf-8"))


def window(whole: str, *, start: int = 0, budget: int = READ_BUDGET_BYTES) -> Window:
    """The longest piece of ``whole`` from ``start`` that fits ``budget``
    bytes, ending after its last newline if it has one. A budget of zero
    gives an empty window, which is how a reading that has spent its budget
    still says a text is there."""
    total_chars = len(whole)
    total_bytes = len(whole.encode("utf-8"))
    if start < 0 or start > total_chars:
        raise ValueError("window start outside the text")
    if budget <= 0:
        return Window("", start, start, total_bytes, total_chars)
    # ``budget`` characters hold at least ``budget`` bytes, so this slice
    # holds the answer; trimming it on the encoded side and decoding with
    # ``ignore`` drops only the partial sequence the cut left at the end.
    piece = whole[start : start + budget]
    raw = piece.encode("utf-8")
    if len(raw) > budget:
        piece = raw[:budget].decode("utf-8", errors="ignore")
    end = start + len(piece)
    if end < total_chars:
        newline = piece.rfind("\n")
        if newline >= 0:
            end = start + newline + 1
    return Window(whole[start:end], start, end, total_bytes, total_chars)


def shares(sizes: Sequence[int], budget: int = READ_BUDGET_BYTES) -> list[int]:
    """Split ``budget`` among texts of ``sizes`` bytes so that every text
    that fits its fair share is whole and the rest split what is left
    evenly: a short source does not waste the room a long one needs."""
    out = [0] * len(sizes)
    left = budget
    pending = sorted(range(len(sizes)), key=lambda i: sizes[i])
    while pending:
        fair = left // len(pending)
        i = pending.pop(0)
        out[i] = min(sizes[i], fair)
        left -= out[i]
    return out


def cursor(w: Window, digest: str) -> str | None:
    """The cursor that continues after ``w`` on the text whose SHA-256 is
    ``digest``, or None when ``w`` reached the end."""
    if w.next_start is None:
        return None
    return encode_cursor([w.next_start, digest[:_DIGEST_CHARS]])


def resume(token: str | None, whole: str, *, version: int, operation: str) -> int:
    """Where the window a cursor names starts, after checking the cursor was
    cut from ``whole`` as it is now. None starts at the beginning. A cursor
    from an earlier text is a stale read: ``concurrency.stale_version`` with
    ``version``, so the caller re-reads from the start."""
    if token is None:
        return 0
    try:
        raw = decode_cursor(token)
    except ValueError as exc:
        raise _invalid(operation) from exc
    start, digest = raw if len(raw) == 2 else (None, None)
    if not isinstance(start, int) or isinstance(start, bool) or not isinstance(digest, str):
        raise _invalid(operation)
    if body_sha256(whole)[:_DIGEST_CHARS] != digest:
        raise_stale_version(version)
    if not 0 <= start <= len(whole):
        raise _invalid(operation)
    return start


def _invalid(operation: str) -> UnprocessableError:
    return UnprocessableError(
        MessageCode.OPERATION_INVALID_ARGUMENT,
        operation=operation,
        detail="cursor: not one this read issued",
    )


__all__ = ["READ_BUDGET_BYTES", "Window", "cursor", "resume", "shares", "window"]
