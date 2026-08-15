"""Shadow mode for payment connectors: compose and validate, never send.

ADR-0051 shipped three automation modes (``transmit`` / ``draft`` / ``off``).
None of them answers the question an operator actually has before cutting over
from an incumbent e-invoicing provider: *would the documents this thing
produces be correct?* ``draft`` composes but never builds an XML -- the
document is frozen at transmit (ADR-0046) -- so there is nothing to inspect and
nothing to diff. ``transmit`` answers the question by filing real documents,
which is the risk being avoided.

``dry_run`` closes that gap. It runs the entire real path -- counterpart
resolution into the anagrafica, line composition, totals, the FatturaPA build,
the official XSD validation -- and stops one step before SdI. It allocates NO
fiscal number: the XML carries the would-be number and the ``ANTEPRIMA``
progressivo that ``get_xml_preview`` already produces, so a shadow run cannot
consume a sequence a real document will later need.

Two columns on the event, not on the invoice:

- ``dry_run`` records that THIS event was shadowed. On the event rather than
  inferred from the connector because the mode is a setting that changes: after
  the flag comes off, the events processed while shadowing must stay
  identifiable as such.
- ``dry_run_xml`` freezes the generated document. The whole value of a parallel
  run is comparing a fixed artefact against the incumbent's output, and a
  preview regenerated on demand would silently reflect data edited since. It is
  deliberately NOT put on ``invoices.xml``: that column means "this is the
  document that was filed", and writing a never-sent XML there would make a
  shadow document indistinguishable from a transmitted one.

Revision ID: 0093
Revises: 0092
Create Date: 2026-08-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0093"
down_revision: str | None = "0092"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MODES_OLD = "'transmit','draft','off'"
_MODES_NEW = "'transmit','draft','dry_run','off'"


# The real constraint names carry a DOUBLED prefix. 0092 passed an
# already-complete name to ``sa.CheckConstraint(name=...)`` inside
# ``op.create_table``, and the metadata naming convention
# ``ck_%(table_name)s_%(constraint_name)s`` prefixed it again -- the same trap
# ``ck_entity_revision_ck_entity_revision_actor_kind`` fell into. Renaming them
# would be churn on a live table for no behavioural gain, so the names stand and
# every later migration has to spell them the way the database really has them.
def _ck(column: str) -> str:
    return f"ck_payment_connectors_ck_payment_connectors_{column}"


def upgrade() -> None:
    # A shadow claim must be invisible to a live run, and the discriminator has
    # to be a COLUMN. The first cut prefixed the provider object id with a
    # reserved string, which is unsound: on the native contract the id is the
    # sender's own ``reference``, so a sender could present the reserved form
    # and make a live run resolve to a shadow document -- exactly the
    # double-emission the separation exists to prevent. A boolean nobody
    # outside this service can influence has no such surface.
    op.add_column(
        "payment_object_links",
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.drop_constraint("uq_payment_object_links_object", "payment_object_links", type_="unique")
    op.create_unique_constraint(
        "uq_payment_object_links_object",
        "payment_object_links",
        ["connector_id", "object_kind", "object_id", "dry_run"],
    )
    op.add_column(
        "payment_connector_events",
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "payment_connector_events",
        sa.Column("dry_run_xml", sa.Text(), nullable=True),
    )
    for column in ("invoice_mode", "credit_note_mode"):
        name = _ck(column)
        op.execute(f"ALTER TABLE payment_connectors DROP CONSTRAINT {name}")
        op.execute(
            f"ALTER TABLE payment_connectors ADD CONSTRAINT {name} "
            f"CHECK ({column} IN ({_MODES_NEW}))"
        )


def downgrade() -> None:
    # Any connector left in the new mode has to come back to a mode the old
    # CHECK admits, or the constraint cannot be re-applied. ``draft`` is the
    # honest landing place: it is the closest surviving behaviour (compose, do
    # not send) and, unlike ``transmit``, a downgrade can never turn a
    # deliberately-shadowed connector into one that files real documents.
    op.execute(
        "UPDATE payment_connectors SET invoice_mode = 'draft' WHERE invoice_mode = 'dry_run'"
    )
    op.execute(
        "UPDATE payment_connectors SET credit_note_mode = 'draft' "
        "WHERE credit_note_mode = 'dry_run'"
    )
    for column in ("invoice_mode", "credit_note_mode"):
        name = _ck(column)
        op.execute(f"ALTER TABLE payment_connectors DROP CONSTRAINT {name}")
        op.execute(
            f"ALTER TABLE payment_connectors ADD CONSTRAINT {name} "
            f"CHECK ({column} IN ({_MODES_OLD}))"
        )
    op.drop_column("payment_connector_events", "dry_run_xml")
    op.drop_column("payment_connector_events", "dry_run")
    # Shadow claims cannot survive the narrowing: they would collide with the
    # live claim for the same object under the 3-column unique.
    op.execute("DELETE FROM payment_object_links WHERE dry_run")
    op.drop_constraint("uq_payment_object_links_object", "payment_object_links", type_="unique")
    op.create_unique_constraint(
        "uq_payment_object_links_object",
        "payment_object_links",
        ["connector_id", "object_kind", "object_id"],
    )
    op.drop_column("payment_object_links", "dry_run")
