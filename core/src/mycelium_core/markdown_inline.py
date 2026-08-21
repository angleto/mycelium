"""Building markdown by string interpolation, safely.

Every place the platform hands a caller a paste-ready reference builds it
by interpolation: ``[{label}]({href})``. The label is user data (an
uploaded filename, an entity title), and nothing was escaping it. An
attachment called ``Report ]final.pdf`` produced::

    [Report ]final.pdf](/attachments/<id>/download)

which is not a link: it parses as the link ``[Report ]`` followed by the
literal text ``final.pdf](/attachments/...)``. ``_sanitize_filename``
strips path separators and leading dots only, so ``]``, ``[`` and ``\\``
all reach the emitter intact.

Mirrored, deliberately duplicated, in two other emitters that cannot
share this module: ``web/src/lib/markdownInline.ts`` (the SPA) and
``mycelium_cli.cmds._common`` (the CLI ships standalone, with no
dependency on core). Change one, change all three.
"""

from __future__ import annotations

import re

_LABEL_NEWLINES = re.compile(r"\s*[\r\n]+\s*")
_DEST_NEEDS_WRAP = re.compile(r"[\s()<>\\]")


def md_link_label(text: str) -> str:
    """Escape ``text`` for use between ``[`` and ``]``.

    Backslash first, so the escapes added next are not re-escaped, then
    the brackets. Newline runs collapse to a single space: a label may
    legally wrap in CommonMark, but a blank line inside it ends the
    paragraph and truncates the link, and every reference emitted here is
    a one-liner anyway.
    """
    escaped = text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    return _LABEL_NEWLINES.sub(" ", escaped)


def md_link_destination(href: str) -> str:
    """Escape ``href`` for use between ``(`` and ``)``.

    A destination holding whitespace or parentheses has to be wrapped in
    angle brackets, and then ``<``, ``>`` and ``\\`` need escaping inside
    the wrapper. Every destination emitted today is already safe, so this
    is defence in depth: it stops a future caller from silently emitting a
    broken link.
    """
    if not _DEST_NEEDS_WRAP.search(href):
        return href
    inner = re.sub(r"([\\<>])", r"\\\1", href)
    return f"<{inner}>"


def md_link(label: str, href: str, *, image: bool = False) -> str:
    """``[label](href)``, or ``![label](href)`` when ``image``. Both parts
    escaped."""
    bang = "!" if image else ""
    return f"{bang}[{md_link_label(label)}]({md_link_destination(href)})"
