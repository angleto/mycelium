"""Note multi-part + per-user collapse state (parent task c0459c4b,
design note 2d228758, Phase 1 = task 801ef530).

Two new tables and one new nullable column:

- ``note_part`` rows are ordered markdown blocks belonging to a note.
  The unique ``(note_id, ord)`` constraint is DEFERRABLE INITIALLY
  DEFERRED so the SPA's reorder transaction can shuffle a whole
  ordering in one go without rolling through a "next free ord"
  scratchpad.

- ``note_part_ui_state`` is user-scoped: each user can collapse the
  parts they don't want to see right now; the row syncs cross-device
  (SPA, mycelium-nvim, future iOS) so the same gardener sees a
  consistent layout everywhere. Defaults are NOT materialised at
  write time: the absence of a row means "expanded" (the SPA reads
  the row, falls back to ``collapsed=false`` on miss).

- ``blob_sources.part_id`` is a nullable FK to ``note_part`` so the
  retrieval / chunking pipeline (NoteTurnChunker + paragraph chunker)
  can record which part a chunk belongs to. Existing chunks point at
  the part the backfill creates; chunks not belonging to a multi-part
  note keep ``NULL``.

The backfill creates exactly one part per note with non-empty
transcript: ``ord=0, body=notes.transcript, lang=NULL,
merged_from_note_id=NULL``. ``notes.transcript`` stays as the source
of truth in this phase --- Phase 6 (task 1cd8bc0a) drops the column
and rewires every consumer (RAG, MCP, SPA, mycelium-cli) to read parts
instead, in a separate PR to keep the blast radius small.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    # --- note_part --------------------------------------------------
    op.create_table(
        "note_part",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "note_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("notes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=sa.text("''")),
        # ISO 639-1 language tag (2 chars typical, allow up to 16 for
        # extended subtags like "pt-BR"). NULL = unspecified / mixed.
        sa.Column("lang", sa.String(16), nullable=True),
        sa.Column(
            "merged_from_note_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
    )
    # Reorder transactions need DEFERRABLE so the client can swap
    # ords without going through a temporary "next free" value.
    op.execute(
        "ALTER TABLE note_part "
        "ADD CONSTRAINT uq_note_part_note_id_ord UNIQUE (note_id, ord) "
        "DEFERRABLE INITIALLY DEFERRED"
    )
    op.create_index("ix_note_part_note_id", "note_part", ["note_id", "ord"])
    op.create_index("ix_note_part_org_id", "note_part", ["org_id"])

    op.execute("ALTER TABLE note_part ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE note_part FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_note_part ON note_part USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE note_part TO mycelium_app")

    # --- note_part_ui_state -----------------------------------------
    # User-scoped: every gardener owns their own collapse state per
    # part. No org_id column on the row itself (the part_id FK
    # already pulls it through the note); RLS still gates the table
    # because a leak of "user X has collapsed part Y" would betray
    # workspace structure. The policy joins note_part -> notes for the
    # org check.
    op.create_table(
        "note_part_ui_state",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "part_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("note_part.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "collapsed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("user_id", "part_id", name="pk_note_part_ui_state"),
    )
    op.execute("ALTER TABLE note_part_ui_state ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE note_part_ui_state FORCE ROW LEVEL SECURITY")
    # The row is reachable iff the user can see the underlying part's
    # note (org check). EXISTS over the join keeps the predicate
    # boolean for both USING and WITH CHECK.
    op.execute(
        "CREATE POLICY p_note_part_ui_state ON note_part_ui_state "
        "USING (EXISTS ("
        "  SELECT 1 FROM note_part np "
        "    WHERE np.id = note_part_ui_state.part_id "
        f"      AND np.{_ORG_PRED}"
        ")) WITH CHECK (EXISTS ("
        "  SELECT 1 FROM note_part np "
        "    WHERE np.id = note_part_ui_state.part_id "
        f"      AND np.{_ORG_PRED}"
        "))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE note_part_ui_state TO mycelium_app")

    # --- blob_sources.part_id ---------------------------------------
    # Nullable because (a) blobs whose source is not a note (tasks,
    # consolidated, agent) have no part to point at; (b) a multi-step
    # rollout shouldn't require backfilling every chunk before the
    # column exists. The backfill below populates the column for the
    # note-blobs we just created parts for.
    op.add_column(
        "blob_sources",
        sa.Column("part_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_blob_sources_part_id_note_part",
        "blob_sources",
        "note_part",
        ["part_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_blob_sources_part_id",
        "blob_sources",
        ["part_id"],
        postgresql_where=sa.text("part_id IS NOT NULL"),
    )

    # --- Backfill ---------------------------------------------------
    # Every note with a non-empty transcript gets exactly one part
    # (ord=0). Notes whose transcript is NULL or "" get nothing --
    # they'll grow parts when the user (or the API) writes the first
    # body, which is the natural moment to materialise the row.
    #
    # CRITICAL: the migration runs as the table owner (``mycelium``) but
    # the tables carry FORCE ROW LEVEL SECURITY, so even the owner
    # is gated by the ``app.current_org`` GUC -- and the migration
    # runs WITHOUT a GUC (no tenant scope). Without the
    # NO FORCE / FORCE bracket below, the INSERT...SELECT would see
    # zero rows (RLS fail-closed) and silently no-op. This is the
    # bug that emptied prod note bodies before the fix: see the
    # 2026-05-27 incident comment on task 1cd8bc0a.
    op.execute("ALTER TABLE notes NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE note_part NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE blob_sources NO FORCE ROW LEVEL SECURITY")
    try:
        op.execute(
            """
            INSERT INTO note_part (org_id, note_id, ord, body, lang)
            SELECT n.org_id, n.id, 0, n.transcript, NULL
              FROM notes n
             WHERE n.transcript IS NOT NULL
               AND n.transcript <> ''
            """
        )
        # Backfill blob_sources.part_id: every note-blob whose source
        # points at a note that just got a part finds its way to
        # part 0. Use the canonical (source_kind, source_id) pair the
        # chunker writes. Out-of-scope sources (task / consolidated /
        # agent) keep NULL because they have no note ancestor.
        op.execute(
            """
            UPDATE blob_sources bs
               SET part_id = np.id
              FROM note_part np
             WHERE bs.source_kind = 'note'
               AND np.ord = 0
               AND np.note_id::text = bs.source_id
               AND bs.part_id IS NULL
            """
        )
    finally:
        op.execute("ALTER TABLE blob_sources FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE note_part FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE notes FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_blob_sources_part_id")
    op.drop_constraint("fk_blob_sources_part_id_note_part", "blob_sources", type_="foreignkey")
    op.drop_column("blob_sources", "part_id")

    op.execute("DROP POLICY IF EXISTS p_note_part_ui_state ON note_part_ui_state")
    op.drop_table("note_part_ui_state")

    op.execute("DROP POLICY IF EXISTS p_note_part ON note_part")
    op.drop_index("ix_note_part_org_id", table_name="note_part")
    op.drop_index("ix_note_part_note_id", table_name="note_part")
    op.execute("ALTER TABLE note_part DROP CONSTRAINT IF EXISTS uq_note_part_note_id_ord")
    op.drop_table("note_part")
