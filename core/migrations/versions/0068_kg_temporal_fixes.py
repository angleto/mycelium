"""KG temporal-correctness + GDPR/immutability hardening (ADR-0044 follow-up).

Fixes found by adversarially verifying migration 0067:

1. "One current fact per triple" now keys the OPEN current fact
   (``invalidated_at IS NULL AND valid_to IS NULL``). Under 0067 the partial
   unique keyed only ``invalidated_at IS NULL`` -- but ``supersede_fact`` closes
   ``valid_to`` WITHOUT invalidating, so a superseded row kept occupying the
   slot. That made re-asserting a prior triple ("re-hire": A -> B -> A) and
   storing several closed historical windows for one triple impossible. The
   open-fact predicate frees both while still forbidding two live facts.

2. ``ck_kg_edge_valid_window`` tightened to STRICT ``valid_to > valid_from``. A
   zero-width window (``valid_to == valid_from``) is unsatisfiable by the
   half-open read predicate (``valid_from <= t AND valid_to > t``) -- it would
   be stored yet invisible. Reject it at write.

3. Invalidate-not-delete hardened against DELETE and against RI-wedging.
   - The 0067 BEFORE UPDATE freeze raised on ANY update of an invalidated row,
     including the RI ``ON DELETE SET NULL`` of ``source_note_id`` when a source
     note is hard-deleted -- which would WEDGE note deletion. It now raises only
     for a DIRECT app update (``pg_trigger_depth() = 1``); RI-cascade updates
     (deeper) pass.
   - A new BEFORE DELETE trigger blocks DIRECT app deletion of history; RI
     cascades (org delete; entity delete) pass (deeper depth), and the
     authorized GDPR erase path opts in via the ``app.kg_allow_erase`` GUC. So
     tombstoned history can no longer be silently rewritten OR deleted, yet
     erase-by-provenance and tenant teardown still work.

Revision ID: 0068
Revises: 0067
Create Date: 2026-06-30
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # (1) Re-scope the partial-unique to the OPEN current fact.
    op.drop_index("uq_kg_edge_current", table_name="kg_edge")
    op.execute(
        "CREATE UNIQUE INDEX uq_kg_edge_current ON kg_edge "
        "(org_id, subject_id, predicate, object_id) "
        "WHERE invalidated_at IS NULL AND valid_to IS NULL"
    )

    # (2) Strict valid-window: a zero-width window is invisible -> forbid it.
    op.drop_constraint("ck_kg_edge_valid_window", "kg_edge", type_="check")
    op.create_check_constraint(
        "ck_kg_edge_valid_window",
        "kg_edge",
        "valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from",
    )

    # (3a) UPDATE freeze: block only a DIRECT app rewrite of invalidated history;
    # allow RI-cascade updates (e.g. source_note_id -> NULL on note delete).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION kg_edge_no_update_invalidated()
        RETURNS TRIGGER AS $$
        BEGIN
          IF OLD.invalidated_at IS NOT NULL AND pg_trigger_depth() = 1 THEN
            RAISE EXCEPTION
              'kg_edge % is invalidated history and cannot be updated', OLD.id
              USING ERRCODE = 'check_violation';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )

    # (3b) DELETE freeze: history (and live rows) cannot be DELETED directly by
    # the app; only the authorized erase path (GUC) or an RI cascade (deeper
    # trigger depth: org/entity teardown) may delete.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION kg_no_uncontrolled_delete()
        RETURNS TRIGGER AS $$
        BEGIN
          IF pg_trigger_depth() = 1
             AND COALESCE(current_setting('app.kg_allow_erase', true), '') <> 'on' THEN
            RAISE EXCEPTION
              '% rows are append-only history; use the erase-by-provenance path',
              TG_TABLE_NAME
              USING ERRCODE = 'check_violation';
          END IF;
          RETURN OLD;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_kg_edge_no_delete BEFORE DELETE ON kg_edge "
        "FOR EACH ROW EXECUTE FUNCTION kg_no_uncontrolled_delete()"
    )
    op.execute(
        "CREATE TRIGGER trg_kg_entity_no_delete BEFORE DELETE ON kg_entity "
        "FOR EACH ROW EXECUTE FUNCTION kg_no_uncontrolled_delete()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_kg_entity_no_delete ON kg_entity")
    op.execute("DROP TRIGGER IF EXISTS trg_kg_edge_no_delete ON kg_edge")
    op.execute("DROP FUNCTION IF EXISTS kg_no_uncontrolled_delete()")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION kg_edge_no_update_invalidated()
        RETURNS TRIGGER AS $$
        BEGIN
          IF OLD.invalidated_at IS NOT NULL THEN
            RAISE EXCEPTION
              'kg_edge % is invalidated history and cannot be updated', OLD.id
              USING ERRCODE = 'check_violation';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.drop_constraint("ck_kg_edge_valid_window", "kg_edge", type_="check")
    op.create_check_constraint(
        "ck_kg_edge_valid_window",
        "kg_edge",
        "valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from",
    )
    op.drop_index("uq_kg_edge_current", table_name="kg_edge")
    op.execute(
        "CREATE UNIQUE INDEX uq_kg_edge_current ON kg_edge "
        "(org_id, subject_id, predicate, object_id) WHERE invalidated_at IS NULL"
    )
