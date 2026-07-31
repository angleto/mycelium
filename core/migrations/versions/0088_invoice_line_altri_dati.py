"""AltriDatiGestionali (FatturaPA 2.2.1.16) as a typed child of invoice_lines.

``DettaglioLinee`` carries ``AltriDatiGestionali`` minOccurs=0
maxOccurs=unbounded, so the block is 0..N *per line*, ordered: the XSD
sequence is positional and the receiver reads the blocks in document
order. That is a child table, one row per emitted block, with an
explicit ``ord`` -- not a JSONB bag: ADR-0003 rejects free JSONB for
fiscally sensitive data precisely because it throws away the
constraints and the validation the law requires, and this block feeds
the XML that SdI validates against
``services/fatturapa_xsd/Schema_VFPA12_V1.2.3.xsd``.

Shape mirrors the XSD complexType AltriDatiGestionaliType:
    TipoDato          String10Type       (\\p{IsBasicLatin}{1,10})  required
    RiferimentoTesto  String60LatinType  (Latin-1, 1..60)          optional
    RiferimentoNumero Amount8DecimalType ([-]?[0-9]{1,11}\\.[0-9]{2,8}) opt
    RiferimentoData   xs:date                                       optional

Org-scoped with FORCE ROW LEVEL SECURITY and the same ``p_<table>``
policy every tenant table carries (0001_baseline.sql, ADR-0002/0015):
without it the table is either invisible to ``mycelium_app`` or a
cross-tenant leak. ``org_id`` is functionally determined by the parent
line but is stored anyway, because the policy predicate has to be
evaluable on the row itself.

The parent FK is ON DELETE CASCADE: an invoice line only ever
disappears with its draft invoice (ADR-0009 -- a transmitted document
is immutable and its XML is frozen in ``Invoice.xml``), so its blocks
must go with it rather than dangle.

Revision ID: 0088
Revises: 0087
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0088"
down_revision: str | None = "0087"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "invoice_line_altri_dati"
_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        # gen_random_uuid() like invoice_lines: the app supplies the pk
        # (UUIDPKMixin, default=uuid4), the server default only keeps raw
        # SQL inserts (psql, an importer) working.
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
            "invoice_line_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoice_lines.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Position of the block within its line. The XSD sequence is
        # ordered and SdI re-reads the blocks in document order, so the
        # emitted order must be persisted, never left to the planner.
        sa.Column("ord", sa.Integer(), nullable=False),
        # TipoDato is a LABEL naming the kind of data (INTENTO,
        # N.DOC.COMM, NB3, ...), not a description; the free text is
        # RiferimentoTesto. The spec fixes no enum, so no CHECK IN (...)
        # here: a closed list at the storage layer would reject the
        # conventions this table does not yet know about.
        sa.Column("tipo_dato", sa.String(length=10), nullable=False),
        sa.Column("riferimento_testo", sa.String(length=60), nullable=True),
        # Amount8DecimalType keeps up to 8 decimals; Numeric(21,8) holds
        # every value the XSD admits (its 11 integer digits are bounded
        # by ck_..._riferimento_numero_range below, since 21-8=13 would
        # otherwise let a value in that can never be serialised).
        sa.Column("riferimento_numero", sa.Numeric(21, 8), nullable=True),
        sa.Column("riferimento_data", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # One block per position, so a re-save cannot duplicate a slot and
        # the emitted order is total. Same shape as
        # uq_invoice_lines_invoice_id (invoice_id, line_no) on the parent.
        sa.UniqueConstraint("invoice_line_id", "ord", name="uq_invoice_line_altri_dati_ord"),
        # Bare constraint names: the metadata naming convention
        # (models/base.py) expands "ck" to ck_%(table_name)s_%(constraint_name)s,
        # and alembic applies it inside create_table too -- spelling the
        # prefix here would yield ck_<table>_ck_<table>_... (see
        # ck_webhook_endpoints_ck_webhook_endpoints_name_len, 0083).
        #
        # Length only, not charset. String10Type/String60LatinType also
        # restrict the alphabet, but an out-of-alphabet character is user
        # input and has to fail with a stable MessageCode (ADR-0017), not
        # with a raw 23514; the service layer owns that check. Length is
        # kept here as well because a truncated fiscal reference is a
        # silent corruption, and varchar(N) alone would not catch ''.
        sa.CheckConstraint(
            "length(tipo_dato) >= 1 AND length(tipo_dato) <= 10",
            name="tipo_dato_len",
        ),
        # NULL = the element is absent (NB3 leaves all three optional
        # fields empty). '' is NOT absent: it would emit an empty
        # <RiferimentoTesto/>, which String60LatinType's {1,60} rejects
        # and SdI scarta. Blank must be normalised to NULL before insert.
        sa.CheckConstraint(
            "riferimento_testo IS NULL"
            " OR (length(riferimento_testo) >= 1 AND length(riferimento_testo) <= 60)",
            name="riferimento_testo_len",
        ),
        # Amount8DecimalType allows at most 11 integer digits.
        sa.CheckConstraint(
            "riferimento_numero IS NULL OR abs(riferimento_numero) < 100000000000",
            name="riferimento_numero_range",
        ),
    )
    # Parent lookup ("the blocks of this line"), mirroring
    # ix_invoice_lines_invoice_id on the parent table.
    op.create_index("ix_invoice_line_altri_dati_invoice_line_id", _TABLE, ["invoice_line_id"])
    # org_id index like every OrgScoped table (OrgScopedMixin index=True);
    # the RLS predicate filters on it on every access.
    op.create_index("ix_invoice_line_altri_dati_org_id", _TABLE, ["org_id"])

    # RLS exactly as 0001_baseline.sql sets it for invoice_lines: ENABLE +
    # FORCE (so the owner role is not exempt), one p_<table> policy with
    # the org predicate on both USING and WITH CHECK, and the CRUD grant
    # to the app role -- which has no rights on a freshly created table.
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY p_{_TABLE} ON {_TABLE} USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {_TABLE} TO mycelium_app")


def downgrade() -> None:
    # Full inverse: the policy first (DROP TABLE would take it along, but
    # dropping it explicitly keeps the reverse readable and idempotent),
    # then the table -- which takes its indexes, constraints and grants.
    op.execute(f"DROP POLICY IF EXISTS p_{_TABLE} ON {_TABLE}")
    op.drop_table(_TABLE)
