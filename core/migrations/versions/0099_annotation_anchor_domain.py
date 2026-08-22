"""An inline annotation's anchor moves to the domain the document is in.

An annotation pins itself to a passage with a W3C-style triple: the quoted
text plus a little context on each side. Until now that triple was captured
in the editor's RENDERED domain -- markdown stripped, links reduced to their
label, blocks joined by a single space -- because the SPA read it off a
ProseMirror tree while the body was stored as markdown source. Two domains,
so two projections of the same document had to agree: 269 lines of TypeScript
building rendered-text-with-a-position-map from the editor tree, and ~350
lines of Python building the same thing from the source with markdown-it,
whose docstring says outright that it "mirrors the editor's markdown-it
configuration".

The markdown editor's document IS the markdown now. A selection is a source
span, so the two domains have collapsed into one and locating an anchor is
``str.find`` on the body.

Existing rows are CONVERTED, not dual-pathed. The conversion is deterministic
rather than a guess: ``md_anchor.resolve_anchor`` already returns SOURCE
offsets for a rendered anchor, so ``body[src_start:src_end]`` is exactly the
source text that rendered quote covers. Each row is re-anchored against the
body it points at, and its context is re-taken from that same body in the
same 24-character window the SPA uses, so a migrated anchor and a freshly
captured one are indistinguishable to the locator.

A row whose rendered anchor no longer resolves is left at ``rendered``. It is
not a loss: such a row is already un-paintable and un-acceptable today, for
the same reason it fails to convert. Marking it keeps the fact that it was
never converted, rather than relabelling it and letting the source locator
read it in a domain it was not written in -- which would turn a visibly stale
anchor into one that might match the wrong passage.

General comments (``anchor_quote IS NULL``) carry no anchor and are untouched.

Revision ID: 0099
Revises: 0098
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0099"
down_revision: str | None = "0098"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "comments",
        sa.Column(
            "anchor_domain",
            sa.String(length=16),
            nullable=False,
            server_default="source",
        ),
    )
    # Everything that exists right now was captured in the rendered domain;
    # the conversion below promotes what it can.
    op.execute(
        sa.text("UPDATE comments SET anchor_domain = 'rendered' WHERE anchor_quote IS NOT NULL")
    )
    _convert_rendered_anchors()


def downgrade() -> None:
    # The quotes are not converted back. Going the other way would mean
    # re-deriving a rendered quote from a source one, which is the direction
    # that is NOT deterministic, and a wrong quote is worse than a dropped
    # column: it would point an annotation at a passage nobody chose.
    op.drop_column("comments", "anchor_domain")


def _convert_rendered_anchors() -> None:
    """Re-anchor every rendered-domain row against the body it points at."""
    from mycelium_core.services import md_anchor

    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            """
            SELECT c.id,
                   c.anchor_quote,
                   c.anchor_prefix,
                   c.anchor_suffix,
                   COALESCE(np.body, t.description) AS body
              FROM comments c
              LEFT JOIN note_part np ON np.id = c.note_part_id
              LEFT JOIN tasks t ON t.id = c.task_id
             WHERE c.anchor_quote IS NOT NULL
            """
        )
    ).fetchall()

    converted = 0
    for row in rows:
        body = row.body
        if not body:
            continue
        try:
            triple = md_anchor.source_quote_for(
                body,
                original=row.anchor_quote,
                prefix=row.anchor_prefix,
                suffix=row.anchor_suffix,
            )
        except Exception:
            # A body this module cannot parse is a row we leave alone, not a
            # migration that stops halfway through somebody's workspace.
            triple = None
        if triple is None:
            continue
        quote, prefix, suffix = triple
        conn.execute(
            sa.text(
                """
                UPDATE comments
                   SET anchor_quote = :q,
                       anchor_prefix = :p,
                       anchor_suffix = :s,
                       anchor_domain = 'source'
                 WHERE id = :id
                """
            ),
            {"q": quote, "p": prefix, "s": suffix, "id": row.id},
        )
        converted += 1
    print(f"0099: re-anchored {converted}/{len(rows)} annotation(s) to the source domain")
