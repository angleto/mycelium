"""Strict unified-diff apply for markdown text blocks.

A sibling of :mod:`mycelium_core.services.md_anchor`: a pure, DB-free,
"faithful or raise, never corrupt" primitive. It applies a STANDARD
unified diff (what ``diff -u old new`` or ``git diff --no-index`` emit)
to a base string and returns the new string, or raises -- it never
produces a partially-applied or fuzzily-matched result.

Why hand-rolled and not a library: the strictness IS the contract. A
fuzzy applier (``whatthepatch.apply_diff``, ``diff-match-patch``) would
silently relocate or skip a hunk; we must reject instead. The unified
diff grammar is small and fully specified, so we parse it ourselves and
control every rejection, exactly as ``md_anchor`` hand-rolls its renderer
rather than pull a fuzzy markdown lib.

The base gate (:func:`assert_base`) pairs the applier with the
``X-Body-SHA256`` header the raw-download routes emit: the client diffs
against the exact body it downloaded, and the server refuses to apply
unless the live body still hashes to that digest.

Public surface:
- :func:`body_sha256` -- the single digest function shared by the
  ``GET .../body/raw`` header and the gate, so client and server hash
  byte-identical input.
- :func:`apply_unified_diff` -- pure applier, raises :class:`PatchError`.
- :func:`assert_base` -- the sha256 half of the gate, raises
  :class:`PatchBaseMismatch`.
- :func:`apply_patch_text` -- convenience used by the service layer:
  gate + apply + translate :class:`PatchError` to the domain-error family
  (409 / 422 / 400).
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field

from mycelium_core.i18n import MessageCode

# A unified-diff hunk header:
# ``@@ -<old_start>[,<old_count>] +<new_start>[,<new_count>] @@[ section]``.
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# Top-level (outside a hunk body) lines we tolerate and skip as diff
# preamble. Anything else non-empty at top level is garbage -> malformed.
# NB: these are matched ONLY at top level; inside a hunk body the leading
# char is the op (' ', '-', '+', '\\'), so a removed line whose content
# starts with "--- " is never confused with a file header.
_HEADER_PREFIXES: tuple[str, ...] = (
    "diff --git ",
    "diff ",
    "index ",
    "--- ",
    "+++ ",
    "old mode ",
    "new mode ",
    "new file mode ",
    "deleted file mode ",
    "similarity index ",
    "dissimilarity index ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
)


class PatchError(Exception):
    """Base for unified-diff apply failures. Carries a stable
    :class:`MessageCode` (via the ``CODE`` class attribute) plus optional
    params, so the service layer translates it to the right domain error
    mechanically. Plain ``Exception`` (not ``DomainError``) so this module
    stays HTTP-agnostic and the applier itself imports no error policy."""

    CODE: MessageCode

    def __init__(self, detail: str = "", /, **params: object) -> None:
        self.detail = detail
        self.params = params
        super().__init__(detail or self.CODE.value)

    @property
    def code(self) -> MessageCode:
        return self.CODE


class PatchMalformed(PatchError):
    """The patch text is not a parseable unified diff (bad ``@@`` header,
    truncated/over-long hunk, illegal line prefix, binary marker, more
    than one file, or zero hunks)."""

    CODE = MessageCode.PATCH_MALFORMED


class PatchDoesNotApply(PatchError):
    """The patch parsed, but a context/removed line did not match the live
    body at the hunk position (or a hunk ran past EOF, or hunks were out of
    order). All-or-nothing: nothing was produced."""

    CODE = MessageCode.PATCH_DOES_NOT_APPLY


class PatchBaseMismatch(PatchError):
    """The live body no longer hashes to the client's ``base_sha256``: the
    document drifted since download. The world moved (409), distinct from a
    broken diff (422)."""

    CODE = MessageCode.PATCH_STALE


class PatchTooLarge(PatchError):
    """The resulting body would exceed the byte cap. Reuses the existing
    body-limit code so the client sees the identical error as the
    full-replace stream path."""

    CODE = MessageCode.BODY_LIMIT_EXCEEDED


def body_sha256(body: str) -> str:
    """Lowercase hex SHA-256 of ``body`` as UTF-8. Single source of truth
    for the ``X-Body-SHA256`` raw-download header and the base gate, so the
    digest the client receives is byte-identically the one checked."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def assert_base(live: str, *, expected_sha256: str) -> None:
    """Raise :class:`PatchBaseMismatch` unless ``sha256(live)`` equals
    ``expected_sha256`` (case-insensitive hex, constant-time compare). The
    VERSION half of the gate is enforced separately by the setter's
    ``optimistic_update`` (``expected_version``)."""
    actual = body_sha256(live)
    if not hmac.compare_digest(actual, expected_sha256.strip().lower()):
        raise PatchBaseMismatch("base sha256 mismatch")


# --- line model ----------------------------------------------------------


def _split_lines(text: str) -> tuple[list[str], bool]:
    """Split on ``\\n`` only (NOT ``str.splitlines``, which also breaks on
    ``\\v \\f \\x1c \\u2028`` etc) and report whether the text ended in a
    newline. Reassembly via :func:`_join_lines` is exact, including CRLF
    bodies (each line keeps its trailing ``\\r``)."""
    if text == "":
        return [], False
    parts = text.split("\n")
    final_newline = parts[-1] == ""
    if final_newline:
        parts.pop()
    return parts, final_newline


def _join_lines(lines: list[str], final_newline: bool) -> str:
    if not lines:
        return ""
    return "\n".join(lines) + ("\n" if final_newline else "")


# --- parse ---------------------------------------------------------------


@dataclass(slots=True)
class _HunkLine:
    op: str  # ' ' context, '-' remove, '+' add
    content: str
    no_newline: bool = False  # a '\ No newline at end of file' marker follows


@dataclass(slots=True)
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[_HunkLine] = field(default_factory=list)


def _parse_hunk_body(
    raw: list[str], i: int, old_count: int, new_count: int
) -> tuple[list[_HunkLine], int]:
    n = len(raw)
    out: list[_HunkLine] = []
    rem_old = old_count
    rem_new = new_count
    while i < n and (rem_old > 0 or rem_new > 0):
        line = raw[i]
        if line.startswith(" "):
            out.append(_HunkLine(" ", line[1:]))
            rem_old -= 1
            rem_new -= 1
        elif line.startswith("-"):
            out.append(_HunkLine("-", line[1:]))
            rem_old -= 1
        elif line.startswith("+"):
            out.append(_HunkLine("+", line[1:]))
            rem_new -= 1
        elif line.startswith("\\"):
            # '\ No newline at end of file' qualifies the previous line.
            if not out:
                raise PatchMalformed("no-newline marker with no preceding line")
            out[-1].no_newline = True
        elif line == "":
            # A blank context line emitted without its leading space (some
            # editors/transports strip trailing whitespace). Treat as
            # context with empty content.
            out.append(_HunkLine(" ", ""))
            rem_old -= 1
            rem_new -= 1
        else:
            raise PatchMalformed(f"illegal hunk line prefix: {line[:1]!r}")
        if rem_old < 0 or rem_new < 0:
            raise PatchMalformed("hunk line counts exceed the @@ header")
        i += 1
    if rem_old != 0 or rem_new != 0:
        raise PatchMalformed("hunk truncated before the @@ header counts were met")
    # Consume a trailing no-newline marker that qualifies the hunk's last line.
    while i < n and raw[i].startswith("\\"):
        if out:
            out[-1].no_newline = True
        i += 1
    return out, i


def _parse(patch: str) -> list[_Hunk]:
    raw = patch.split("\n")
    # Drop the single trailing "" produced by a newline-terminated patch
    # (the near-universal case). Otherwise that artifact would be consumed
    # as a blank context line and could mask a truncated final hunk. A
    # genuine trailing blank context line is emitted as " " (a space), not
    # "", so this only removes the transport artifact. Counts then stay
    # authoritative: a truncated hunk fails its @@ tally.
    if raw and raw[-1] == "":
        raw.pop()
    i, n = 0, len(raw)
    hunks: list[_Hunk] = []
    file_headers = 0
    while i < n:
        line = raw[i]
        if line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if m is None:
                raise PatchMalformed("malformed @@ hunk header")
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            body, i = _parse_hunk_body(raw, i + 1, old_count, new_count)
            hunks.append(_Hunk(old_start, old_count, new_start, new_count, body))
            continue
        if line.startswith("Binary files") or line.startswith("GIT binary patch"):
            raise PatchMalformed("binary patches are not supported")
        if line.startswith(_HEADER_PREFIXES):
            if line.startswith("--- "):
                file_headers += 1
                if file_headers > 1:
                    raise PatchMalformed("patch spans more than one file")
            i += 1
            continue
        if line == "":
            i += 1
            continue
        raise PatchMalformed("unexpected line outside a hunk")
    if not hunks:
        raise PatchMalformed("patch contains no hunks")
    return hunks


# --- apply ---------------------------------------------------------------


def apply_unified_diff(base: str, patch: str, *, max_result_bytes: int | None = None) -> str:
    """Apply a standard unified diff to ``base``, returning the new text.

    ALL-OR-NOTHING: any malformed input or context/removed-line mismatch
    raises and produces nothing. The result is built entirely in memory and
    returned only after the last hunk verifies.

    Raises:
        PatchMalformed: the patch is not a parseable unified diff.
        PatchDoesNotApply: a hunk does not match the live body.
        PatchTooLarge: the result would exceed ``max_result_bytes``.
    """
    hunks = _parse(patch)
    base_lines, base_final_nl = _split_lines(base)
    n_base = len(base_lines)

    out: list[str] = []
    cursor = 0  # index into base_lines of the next un-consumed old line
    for hunk in hunks:
        # 1-based old_start -> 0-based target. A pure insertion (old_count
        # == 0) carries old_start = the count of preceding lines (0 = top),
        # i.e. the insertion index itself.
        target = hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1
        if target < cursor:
            raise PatchDoesNotApply("overlapping or out-of-order hunks")
        if target > n_base:
            raise PatchDoesNotApply("hunk starts past end of file")
        # Copy the untouched gap before this hunk verbatim.
        out.extend(base_lines[cursor:target])
        cursor = target
        for hl in hunk.lines:
            if hl.op == " ":
                if cursor >= n_base or base_lines[cursor] != hl.content:
                    raise PatchDoesNotApply("context line does not match")
                out.append(base_lines[cursor])
                cursor += 1
            elif hl.op == "-":
                if cursor >= n_base or base_lines[cursor] != hl.content:
                    raise PatchDoesNotApply("removed line does not match")
                cursor += 1
            else:  # '+'
                out.append(hl.content)

    # Final-newline state of the NEW file. Markers only ever appear at EOF,
    # so they live in the last hunk. If the last hunk consumed every old
    # line (cursor == n_base), the new file's tail IS that hunk's tail and
    # its last emitted (' '/'+') line's marker decides; otherwise the
    # untouched trailing lines keep the base's newline state.
    last_hunk = hunks[-1]
    old_eof_no_newline = any(hl.no_newline for hl in last_hunk.lines if hl.op in (" ", "-"))
    if old_eof_no_newline and (base_final_nl or cursor != n_base):
        # The diff claims the old file had no trailing newline at EOF, but
        # the base disagrees (has one, or the hunk did not reach EOF).
        raise PatchDoesNotApply("no-newline marker disagrees with the base")
    if cursor == n_base:
        last_emit = None
        for hl in last_hunk.lines:
            if hl.op in (" ", "+"):
                last_emit = hl
        result_final_nl = (not last_emit.no_newline) if last_emit is not None else base_final_nl
    else:
        result_final_nl = base_final_nl

    out.extend(base_lines[cursor:])
    result = _join_lines(out, result_final_nl)
    if max_result_bytes is not None and len(result.encode("utf-8")) > max_result_bytes:
        raise PatchTooLarge("patched body exceeds the cap", max_bytes=max_result_bytes)
    return result


# --- service convenience -------------------------------------------------


def apply_patch_text(
    live: str,
    patch: str,
    *,
    expected_sha256: str,
    max_result_bytes: int | None = None,
) -> str:
    """Gate + apply + translate. Used by the three service helpers
    (note part / task description / annotation) so the ``PatchError`` ->
    domain-error mapping lives in one place:

    - :class:`PatchBaseMismatch` -> ``ConflictError(PATCH_STALE)`` (409)
    - :class:`PatchDoesNotApply` / :class:`PatchMalformed` ->
      ``UnprocessableError`` (422)
    - :class:`PatchTooLarge` -> ``DomainError(BODY_LIMIT_EXCEEDED)`` (400)

    The version half of the gate is left to the caller's setter
    (``optimistic_update`` on ``expected_version``)."""
    from mycelium_core.errors import ConflictError, DomainError, UnprocessableError

    try:
        assert_base(live, expected_sha256=expected_sha256)
        return apply_unified_diff(live, patch, max_result_bytes=max_result_bytes)
    except PatchBaseMismatch as exc:
        raise ConflictError(exc.code, **exc.params) from exc
    except PatchTooLarge as exc:
        raise DomainError(exc.code, **exc.params) from exc
    except (PatchMalformed, PatchDoesNotApply) as exc:
        raise UnprocessableError(exc.code, **exc.params) from exc


__all__ = [
    "PatchBaseMismatch",
    "PatchDoesNotApply",
    "PatchError",
    "PatchMalformed",
    "PatchTooLarge",
    "apply_patch_text",
    "apply_unified_diff",
    "assert_base",
    "body_sha256",
]
