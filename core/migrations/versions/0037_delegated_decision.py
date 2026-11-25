"""Decisions a person handed over to an assistant (ADR-0076, phase B).

``delegated_decision``  one row per decision an assistant took on its own,
                        under the workspace setting ``garden_delegation``:
                        what kind of decision, on what (a synthesis, a link
                        proposal, a fact), the verdict, the assistant, and
                        whether and when a person reversed it. It is the
                        list of what agents decided and the measure of how
                        often a person disagrees. Workspace RLS; the
                        assistant identity and the reverting person are SET
                        NULL, so removing either keeps the record.

``link_proposal.delegated``  true when the decision that closed the proposal
                        was an assistant's: the proposer precision counts a
                        person's judgements only, as the synthesis accept
                        ratio does.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from mycelium_core.migration_rls import create_owner_session_policy

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.add_column(
        "link_proposal",
        sa.Column("delegated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_table(
        "delegated_decision",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("target_kind", sa.String(20), nullable=False),
        sa.Column("target_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("verdict", sa.String(10), nullable=False),
        sa.Column("assistant_identity_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("result_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reversed_by", PG_UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_delegated_decision"),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_delegated_decision_org_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assistant_identity_id"],
            ["identities.id"],
            name="fk_delegated_decision_assistant_identity_id_identities",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reversed_by"],
            ["users.id"],
            name="fk_delegated_decision_reversed_by_users",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "target_kind IN ('synthesis', 'link_proposal', 'fact')",
            name="ck_delegated_decision_target_kind",
        ),
        sa.CheckConstraint(
            "verdict IN ('approve', 'decline')", name="ck_delegated_decision_verdict"
        ),
    )
    op.create_index(
        "ix_delegated_decision_org_created", "delegated_decision", ["org_id", "created_at"]
    )
    op.create_index("ix_delegated_decision_target", "delegated_decision", ["target_id"])
    op.execute("ALTER TABLE delegated_decision ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE delegated_decision FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_delegated_decision ON delegated_decision USING ({_ORG_PRED}) "
        f"WITH CHECK ({_ORG_PRED})"
    )
    create_owner_session_policy(op.get_bind(), "delegated_decision")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE delegated_decision TO mycelium_app")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("REVOKE ALL ON TABLE delegated_decision FROM mycelium_app")
    op.execute("DROP POLICY IF EXISTS owner_session_all ON delegated_decision")
    op.execute("DROP POLICY IF EXISTS p_delegated_decision ON delegated_decision")
    op.drop_table("delegated_decision")
    op.drop_column("link_proposal", "delegated")
