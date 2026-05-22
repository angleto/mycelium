"""Per-intermediary SdI transmission sequence (docs/adr/0011, FR-9 / F7b).

The SdI file name (``IT{idfiscale}_{progressivo}.xml``) and ProgressivoInvio
must be unique per *trasmittente*. One accredited channel transmits for many
tenants, so the sequence is platform-level (keyed by the intermediary's
id_codice), not per-org.

RLS posture: not org-scoped (no org_id); a single shared sequence read/written
from any tenant_session during transmit. ENABLE + USING(true) policy +
flow_app grants (same posture as the operational telegram tables in 0071):
isolation here is irrelevant (it holds only an opaque counter), but the
runtime still goes through a policy rather than an unprotected table.

Revision: 0073
Down revision: 0072
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0073"
down_revision: str | None = "0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    """
    CREATE TABLE sdi_transmission_counters (
      intermediary_id varchar(40) NOT NULL,
      last_number bigint NOT NULL DEFAULT 0,
      CONSTRAINT pk_sdi_transmission_counters PRIMARY KEY (intermediary_id)
    )
    """,
    "ALTER TABLE sdi_transmission_counters ENABLE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_sdi_transmission_counters ON sdi_transmission_counters "
        "USING (true) WITH CHECK (true)"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON sdi_transmission_counters TO flow_app",
)


DOWNGRADE: tuple[str, ...] = ("DROP TABLE IF EXISTS sdi_transmission_counters CASCADE",)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
