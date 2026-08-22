"""Rework note_note_link kinds into the mycelial 4-verb model
(ADR-0040, revises ADR-0029).

The legacy kinds {atom_of, references, replies_to, supersedes} are
replaced by four verbs that match how thinking actually evolves:

  - ``hypha_of``    : A derived / sprouted from B (DIRECTIONAL, genesis).
                      ``atom_of`` folds here; the decomposition
                      pipeline's source -> distillation link is just a
                      ``hypha_of`` too (the distillation derived from its
                      source). Humus is a node facet, not a kind.
  - ``related``     : A and B are simply connected (UNDIRECTED associative
                      weave). ``references`` + ``replies_to`` fold here.
  - ``supersedes``  : A makes B obsolete (directional). The service
                      auto-prunes B to ``dormant`` on link creation.
  - ``contradicts`` : A refutes B as false (directional). Same auto-prune.

Importance (PageRank) is computed UNDIRECTED over the weighted weave, so
a derived idea can outrank the idea that generated it: link direction
carries meaning (genesis / obsolescence) but never authority.

``related`` is undirected, so its rows are canonicalised (parent < child
by id string) and de-duplicated; references+replies_to folding onto the
same pair also collapses. The UNIQUE constraint is dropped for the
rewrite and re-added after de-duplication.

``note_note_link`` is ENABLE-only RLS (not FORCE): the owner role
bypasses its policy and no ``notes`` JOIN is needed, so no NO FORCE /
FORCE bracket.

Downgrade is best-effort and lossy (the fold cannot be inverted):
hypha_of -> atom_of, related -> references, contradicts -> supersedes.

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK = "note_note_link_kind_check"
_UNIQUE = "uq_note_note_link"


def upgrade() -> None:
    op.execute(f"ALTER TABLE note_note_link DROP CONSTRAINT {_CHECK}")
    op.execute(f"ALTER TABLE note_note_link DROP CONSTRAINT {_UNIQUE}")
    # 1. Derivation: atom_of (incl. decomposition source -> distillation).
    op.execute("UPDATE note_note_link SET kind = 'hypha_of' WHERE kind = 'atom_of'")
    # 2. Plain association: citations + replies fold into the weave.
    op.execute(
        "UPDATE note_note_link SET kind = 'related' WHERE kind IN ('references', 'replies_to')"
    )
    # 3. ``related`` is undirected: canonicalise to parent < child (by id
    #    string) so (a, b) and (b, a) are the same edge.
    op.execute(
        """
        UPDATE note_note_link
           SET parent_note_id = child_note_id,
               child_note_id = parent_note_id
         WHERE kind = 'related'
           AND parent_note_id::text > child_note_id::text
        """
    )
    # 4. De-duplicate rows that now collide on (org, parent, child, kind)
    #    (reverse-pair related, or references+replies_to folded onto the
    #    same pair). Keep the lowest id.
    op.execute(
        """
        DELETE FROM note_note_link a
         USING note_note_link b
         WHERE a.org_id = b.org_id
           AND a.parent_note_id = b.parent_note_id
           AND a.child_note_id = b.child_note_id
           AND a.kind = b.kind
           AND a.id > b.id
        """
    )
    # 5. Re-add UNIQUE + the new CHECK (four verbs).
    op.execute(
        f"ALTER TABLE note_note_link ADD CONSTRAINT {_UNIQUE} "
        "UNIQUE (parent_note_id, child_note_id, kind)"
    )
    op.execute(
        f"""
        ALTER TABLE note_note_link ADD CONSTRAINT {_CHECK}
        CHECK (kind = ANY (ARRAY[
            'hypha_of'::text, 'related'::text,
            'supersedes'::text, 'contradicts'::text
        ]))
        """
    )


def downgrade() -> None:
    # Best-effort, lossy: the fold cannot be inverted.
    op.execute(f"ALTER TABLE note_note_link DROP CONSTRAINT {_CHECK}")
    op.execute("UPDATE note_note_link SET kind = 'atom_of' WHERE kind = 'hypha_of'")
    op.execute("UPDATE note_note_link SET kind = 'references' WHERE kind = 'related'")
    op.execute("UPDATE note_note_link SET kind = 'supersedes' WHERE kind = 'contradicts'")
    op.execute(
        f"""
        ALTER TABLE note_note_link ADD CONSTRAINT {_CHECK}
        CHECK (kind = ANY (ARRAY[
            'atom_of'::text, 'references'::text,
            'replies_to'::text, 'supersedes'::text
        ]))
        """
    )
