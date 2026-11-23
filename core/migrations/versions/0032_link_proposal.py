"""Link proposals, remembered rejections, exposure, and the origin of an
accepted link (ADR-0073 decision 7).

``link_proposal``  a suggested note-to-note link from a named proposer, born
    ``proposed``; at most one open proposal per (proposer, source, target).
``link_rejection``  a pair a person rejected, canonical a < b, never proposed
    again by anyone.
``link_proposal_exposure``  each showing of a proposal to a person.
``note_note_link``  gains ``origin_proposal_id``, ``origin_proposer`` and
    ``origin_evidence_ref``: NULL on a link made by hand, set on one accepted
    from a proposal, so the proposer survives the person's acceptance.

Workspace RLS on ``org_id`` for the three new tables. Notes cascade.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from mycelium_core.migration_rls import create_owner_session_policy

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def _tenant_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY p_{table} ON {table} USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})")
    create_owner_session_policy(op.get_bind(), table)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO mycelium_app")


def _id() -> sa.Column[object]:
    return sa.Column(
        "id", PG_UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
    )


def _org_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["org_id"],
        ["organizations.id"],
        name=f"fk_{table}_org_id_organizations",
        ondelete="CASCADE",
    )


def _note_fk(table: str, col: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [col], ["notes.id"], name=f"fk_{table}_{col}_notes", ondelete="CASCADE"
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.create_table(
        "link_proposal",
        _id(),
        sa.Column("org_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("proposer", sa.String(80), nullable=False),
        sa.Column("source_note_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("target_note_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("evidence", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("evidence_ref", sa.Text(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("status", sa.String(10), server_default=sa.text("'proposed'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("accepted_kind", sa.String(20), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_link_proposal"),
        sa.CheckConstraint(
            "status IN ('proposed', 'accepted', 'rejected', 'expired')",
            name="ck_link_proposal_status",
        ),
        sa.CheckConstraint("source_note_id <> target_note_id", name="ck_link_proposal_no_self"),
        _note_fk("link_proposal", "source_note_id"),
        _note_fk("link_proposal", "target_note_id"),
        _org_fk("link_proposal"),
    )
    op.create_index("ix_link_proposal_org_id", "link_proposal", ["org_id"])
    op.create_index(
        "uq_link_proposal_open",
        "link_proposal",
        ["org_id", "proposer", "source_note_id", "target_note_id"],
        unique=True,
        postgresql_where=sa.text("status = 'proposed'"),
    )
    op.create_index(
        "ix_link_proposal_queue", "link_proposal", ["org_id", "status", "source_note_id"]
    )
    _tenant_rls("link_proposal")

    op.create_table(
        "link_rejection",
        _id(),
        sa.Column("org_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("note_a_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("note_b_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("rejected_by", PG_UUID(as_uuid=True), nullable=False),
        sa.Column(
            "rejected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_link_rejection"),
        sa.CheckConstraint("note_a_id < note_b_id", name="ck_link_rejection_canonical"),
        _note_fk("link_rejection", "note_a_id"),
        _note_fk("link_rejection", "note_b_id"),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["link_proposal.id"],
            name="fk_link_rejection_proposal_id_link_proposal",
            ondelete="SET NULL",
        ),
        _org_fk("link_rejection"),
    )
    op.create_index("ix_link_rejection_org_id", "link_rejection", ["org_id"])
    op.create_index(
        "uq_link_rejection_pair",
        "link_rejection",
        ["org_id", "note_a_id", "note_b_id"],
        unique=True,
    )
    _tenant_rls("link_rejection")

    op.create_table(
        "link_proposal_exposure",
        _id(),
        sa.Column("org_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("shown_to", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("surface", sa.String(40), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "shown_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_link_proposal_exposure"),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["link_proposal.id"],
            name="fk_link_proposal_exposure_proposal_id_link_proposal",
            ondelete="CASCADE",
        ),
        _org_fk("link_proposal_exposure"),
    )
    op.create_index("ix_link_proposal_exposure_org_id", "link_proposal_exposure", ["org_id"])
    op.create_index("ix_link_proposal_exposure_proposal", "link_proposal_exposure", ["proposal_id"])
    _tenant_rls("link_proposal_exposure")

    op.add_column(
        "note_note_link", sa.Column("origin_proposal_id", PG_UUID(as_uuid=True), nullable=True)
    )
    op.add_column("note_note_link", sa.Column("origin_proposer", sa.String(80), nullable=True))
    op.add_column("note_note_link", sa.Column("origin_evidence_ref", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_note_note_link_origin_proposal_id_link_proposal",
        "note_note_link",
        "link_proposal",
        ["origin_proposal_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_note_note_link_origin_proposal_id_link_proposal", "note_note_link", type_="foreignkey"
    )
    op.drop_column("note_note_link", "origin_evidence_ref")
    op.drop_column("note_note_link", "origin_proposer")
    op.drop_column("note_note_link", "origin_proposal_id")
    for table in ("link_proposal_exposure", "link_rejection", "link_proposal"):
        op.execute(f"REVOKE ALL ON TABLE {table} FROM mycelium_app")
        op.execute(f"DROP POLICY IF EXISTS owner_session_all ON {table}")
        op.execute(f"DROP POLICY IF EXISTS p_{table} ON {table}")
        op.drop_table(table)
