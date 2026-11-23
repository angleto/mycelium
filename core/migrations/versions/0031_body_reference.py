"""References written in bodies, as a derived projection (ADR-0073).

``body_reference_source``  one row per projected body: a note part or a
    task description, with the SHA-256 of the text the rows were computed
    from. Exactly one of ``note_part_id`` and ``task_id`` is set, and each
    cascades with its row, so a purged part or task leaves nothing behind.
``body_reference``  one row per occurrence: token, syntax, ordinal, target,
    status, rendered anchor, the person's choice on an ambiguous one, and
    when it was first seen. Cascades with its source.

Workspace RLS on ``org_id`` for both, like every tenant table. No backfill
here: the projection is computed by the application (one rule, in
``services.reference_extract``), and ``python -m
mycelium_core.backfill_body_references`` runs it over existing bodies.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from mycelium_core.migration_rls import create_owner_session_policy

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def _tenant_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY p_{table} ON {table} USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})")
    create_owner_session_policy(op.get_bind(), table)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO mycelium_app")


def upgrade() -> None:
    # The foreign keys lock note_part, tasks and organizations in SHARE ROW
    # EXCLUSIVE; bounded so a busy table fails the migration (retried)
    # instead of stalling writes behind it.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.create_table(
        "body_reference_source",
        sa.Column(
            "id", PG_UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("org_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("note_part_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("task_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_body_reference_source"),
        sa.CheckConstraint(
            "(note_part_id IS NULL) <> (task_id IS NULL)",
            name="ck_body_reference_source_one_source",
        ),
        sa.ForeignKeyConstraint(
            ["note_part_id"],
            ["note_part.id"],
            name="fk_body_reference_source_note_part_id_note_part",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_body_reference_source_task_id_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_body_reference_source_org_id_organizations",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_body_reference_source_org_id", "body_reference_source", ["org_id"])
    op.create_index(
        "uq_body_reference_source_part",
        "body_reference_source",
        ["note_part_id"],
        unique=True,
        postgresql_where=sa.text("note_part_id IS NOT NULL"),
    )
    op.create_index(
        "uq_body_reference_source_task",
        "body_reference_source",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("task_id IS NOT NULL"),
    )
    _tenant_rls("body_reference_source")

    op.create_table(
        "body_reference",
        sa.Column(
            "id", PG_UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("org_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("syntax", sa.String(8), nullable=False),
        sa.Column("token", sa.String(36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("target_kind", sa.String(8), nullable=True),
        sa.Column("target_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("chosen_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("chosen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quote_exact", sa.Text(), nullable=True),
        sa.Column("quote_prefix", sa.Text(), nullable=True),
        sa.Column("quote_suffix", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_body_reference"),
        sa.CheckConstraint(
            "syntax IN ('mention', 'chip', 'bare')", name="ck_body_reference_syntax"
        ),
        sa.CheckConstraint(
            "target_kind IS NULL OR target_kind IN ('note', 'task', 'tag')",
            name="ck_body_reference_target_kind",
        ),
        sa.CheckConstraint(
            "status IN ('resolved', 'ambiguous', 'dangling')", name="ck_body_reference_status"
        ),
        sa.CheckConstraint(
            "(status = 'resolved') = (target_id IS NOT NULL AND target_kind IS NOT NULL)",
            name="ck_body_reference_resolved_has_target",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["body_reference_source.id"],
            name="fk_body_reference_source_id_body_reference_source",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_body_reference_org_id_organizations",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_body_reference_org_id", "body_reference", ["org_id"])
    op.create_index(
        "uq_body_reference_occurrence",
        "body_reference",
        ["source_id", "syntax", "token", "ordinal"],
        unique=True,
    )
    op.create_index("ix_body_reference_target", "body_reference", ["org_id", "target_id"])
    _tenant_rls("body_reference")


def downgrade() -> None:
    for table in ("body_reference", "body_reference_source"):
        op.execute(f"REVOKE ALL ON TABLE {table} FROM mycelium_app")
        op.execute(f"DROP POLICY IF EXISTS owner_session_all ON {table}")
        op.execute(f"DROP POLICY IF EXISTS p_{table} ON {table}")
    op.drop_table("body_reference")
    op.drop_table("body_reference_source")
