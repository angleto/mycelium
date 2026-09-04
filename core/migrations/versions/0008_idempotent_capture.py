"""A capture that timed out must not become two.

``api_idempotency`` was built for the public invoice API, where a retry
must never file a second fiscal document. The mechanism is right and it is
proven: an insert-on-conflict-do-nothing in the SAME transaction as the
mutation, a canonical request digest so a reused key with a different body
is refused rather than silently replayed, and a stored snapshot the retry
returns instead of mutating again.

What was not general was the claim's PRINCIPAL. ``issuer_profile_id`` was
NOT NULL, so only a caller holding an issuer key could claim anything --
and the browser extension's capture is exactly the operation that needs
this. It creates a task or a note over a connection that can time out
mid-flight, and a timed-out create has an unknown outcome: the client
cannot tell "it did not arrive" from "it arrived and the answer was lost".
Without a claim the only honest thing a client can offer is "go and look",
and the only dishonest one is a retry button that duplicates work.

A second table would have been a second implementation of one rule, so
this widens the existing one instead. The claim now has two shapes and
exactly one applies per row:

  issuer_profile_id  the invoice API, unchanged, keyed as before
  actor_id           a person acting in a workspace, keyed WITH org_id

The actor branch carries ``org_id`` in its key and the issuer branch does
not, and that asymmetry is deliberate: an issuer profile already implies
one workspace, while the same person retrying in two workspaces with a
client-generated key that happened to collide must not be told their
second capture was a replay of the first.

Partial unique indexes rather than one constraint, because NULLs are
distinct in a unique index: a nullable ``issuer_profile_id`` in the old
constraint would have made every actor claim unique by accident and
deduplicated nothing at all.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "api_idempotency",
        sa.Column("actor_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_api_idempotency_actor_id_users",
        "api_idempotency",
        "users",
        ["actor_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.alter_column("api_idempotency", "issuer_profile_id", nullable=True)

    # One principal per row, enforced by the database rather than by the
    # two call sites: a row with neither claims nothing, and a row with
    # both would be reachable under two different keys.
    op.create_check_constraint(
        "ck_api_idempotency_one_principal",
        "api_idempotency",
        "num_nonnulls(issuer_profile_id, actor_id) = 1",
    )

    op.drop_constraint("uq_api_idempotency_claim", "api_idempotency", type_="unique")
    op.create_index(
        "uq_api_idempotency_issuer_claim",
        "api_idempotency",
        ["issuer_profile_id", "endpoint", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("issuer_profile_id IS NOT NULL"),
    )
    op.create_index(
        "uq_api_idempotency_actor_claim",
        "api_idempotency",
        ["org_id", "actor_id", "endpoint", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("actor_id IS NOT NULL"),
    )


def downgrade() -> None:
    # An actor claim cannot be represented by the old constraint, so it is
    # dropped rather than migrated: replaying a capture is a courtesy, and
    # losing the courtesy on a downgrade costs at worst a duplicate the
    # person can see and delete.
    op.execute(sa.text("DELETE FROM api_idempotency WHERE actor_id IS NOT NULL"))
    op.drop_index("uq_api_idempotency_actor_claim", table_name="api_idempotency")
    op.drop_index("uq_api_idempotency_issuer_claim", table_name="api_idempotency")
    op.drop_constraint("ck_api_idempotency_one_principal", "api_idempotency", type_="check")
    op.alter_column("api_idempotency", "issuer_profile_id", nullable=False)
    op.create_unique_constraint(
        "uq_api_idempotency_claim",
        "api_idempotency",
        ["issuer_profile_id", "endpoint", "idempotency_key"],
    )
    op.drop_constraint("fk_api_idempotency_actor_id_users", "api_idempotency", type_="foreignkey")
    op.drop_column("api_idempotency", "actor_id")
