"""Filenames for ``Content-Disposition``.

Extracted from ``routers/export.py`` when a second downloadable export
appeared: the name is attacker-influenced (it comes from a title or a
workflow name the user typed), so the rule that makes it inert has to be
one rule, applied by everyone who builds an attachment header.
"""

from __future__ import annotations

import re

# Path separators, the Windows reserved set, and control characters --
# a newline reaching a header would let the value forge one of its own.
_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


def slugify_filename(name: str, fallback: str = "untitled") -> str:
    """Reduce ``name`` to something safe to hand a filesystem and a
    header, capped at 120 characters. Falls back when nothing usable is
    left, so the header always carries a name."""
    cleaned = _FORBIDDEN.sub("-", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or fallback)[:120]
