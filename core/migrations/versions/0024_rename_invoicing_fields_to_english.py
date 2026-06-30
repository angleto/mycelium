"""Rename invoicing-domain columns from Italian to English (data-preserving).

The invoicing models (client_profile, issuer_profiles, invoices, invoice_lines)
carried Italian column names; this renames them to English to match the rest
of the codebase convention. Every change is an ``ALTER TABLE ... RENAME COLUMN``
so NO data is lost (the values, types, defaults, nullability and indexes are
preserved -- PostgreSQL renames the column in place, including within its
unique index).

It also normalizes a redundant country prefix in the (now) ``vat_number``
columns: a value like ``IT01112223334`` stored where IdPaese is already ``IT``
becomes ``01112223334`` (the bare IdCodice FatturaPA expects). The XML builder
already strips this at emit time; here we clean the stored value too.

The displayed UI labels are i18n translation strings (unaffected); the
FatturaPA XML element names (IdPaese, Natura, Causale, ...) are the external
SdI spec and stay verbatim -- only our Python/DB identifiers change.
"""

from __future__ import annotations

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


# (table, old_column, new_column)
_RENAMES: list[tuple[str, str, str]] = [
    # --- client_profile ---
    ("client_profile", "ragione_sociale", "legal_name"),
    ("client_profile", "nome", "first_name"),
    ("client_profile", "cognome", "last_name"),
    ("client_profile", "id_paese", "country_code"),
    ("client_profile", "id_codice", "vat_number"),
    ("client_profile", "codice_fiscale", "tax_code"),
    ("client_profile", "indirizzo", "address"),
    ("client_profile", "cap", "postal_code"),
    ("client_profile", "comune", "city"),
    ("client_profile", "provincia", "province"),
    ("client_profile", "nazione", "country"),
    ("client_profile", "codice_destinatario", "sdi_code"),
    ("client_profile", "tariffa", "hourly_rate"),
    ("client_profile", "valuta", "currency"),
    ("client_profile", "default_condizioni_pagamento", "default_payment_conditions_code"),
    ("client_profile", "default_modalita_pagamento", "default_payment_method_code"),
    # --- issuer_profiles ---
    ("issuer_profiles", "regime_fiscale", "tax_regime"),
    ("issuer_profiles", "paese", "country_code"),
    ("issuer_profiles", "piva", "vat_number"),
    ("issuer_profiles", "codice_fiscale", "tax_code"),
    ("issuer_profiles", "denominazione", "legal_name"),
    ("issuer_profiles", "nome", "first_name"),
    ("issuer_profiles", "cognome", "last_name"),
    ("issuer_profiles", "indirizzo", "address"),
    ("issuer_profiles", "cap", "postal_code"),
    ("issuer_profiles", "comune", "city"),
    ("issuer_profiles", "provincia", "province"),
    ("issuer_profiles", "nazione", "country"),
    ("issuer_profiles", "riferimento_normativo", "legal_reference"),
    ("issuer_profiles", "telefono", "phone"),
    ("issuer_profiles", "default_condizioni_pagamento", "default_payment_conditions_code"),
    ("issuer_profiles", "default_modalita_pagamento", "default_payment_method_code"),
    ("issuer_profiles", "codice_destinatario_ricezione", "sdi_code"),
    # --- invoices ---
    ("invoices", "causale", "purpose"),
    ("invoices", "condizioni_pagamento", "payment_conditions_code"),
    ("invoices", "modalita_pagamento", "payment_method_code"),
    ("invoices", "bollo", "stamp_duty"),
    # --- invoice_lines ---
    ("invoice_lines", "natura", "vat_nature"),
    # --- received_invoices (passive SdI cycle) ---
    ("received_invoices", "codice_destinatario", "sdi_code"),
    ("received_invoices", "nome_file", "file_name"),
    ("received_invoices", "formato_trasmissione", "transmission_format"),
    ("received_invoices", "sender_id_paese", "sender_country_code"),
    ("received_invoices", "sender_id_codice", "sender_vat_number"),
    ("received_invoices", "sender_denominazione", "sender_legal_name"),
    ("received_invoices", "committente_verdict", "buyer_verdict"),
    ("received_invoices", "committente_verdict_at", "buyer_verdict_at"),
    # --- notification audit logs ---
    ("invoice_notifications", "nome_file", "file_name"),
    ("received_invoice_notifications", "nome_file", "file_name"),
]


# SECURITY DEFINER resolver used by the inbound/passive SdI path -- its body
# hardcodes the issuer recipient-code column, so it must be rebuilt to match
# the rename (codice_destinatario_ricezione -> sdi_code).
_RESOLVER_NEW = """
CREATE OR REPLACE FUNCTION public.sdi_resolve_recipient_org(p_codice text)
    RETURNS TABLE(org_id uuid, issuer_profile_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
  SELECT org_id, id FROM issuer_profiles
  WHERE sdi_code = p_codice
  LIMIT 1
$$;
"""

_RESOLVER_OLD = """
CREATE OR REPLACE FUNCTION public.sdi_resolve_recipient_org(p_codice text)
    RETURNS TABLE(org_id uuid, issuer_profile_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
  SELECT org_id, id FROM issuer_profiles
  WHERE codice_destinatario_ricezione = p_codice
  LIMIT 1
$$;
"""


def upgrade() -> None:
    for table, old, new in _RENAMES:
        op.alter_column(table, old, new_column_name=new)
    # The partial unique index name still embeds the old column name; rename it
    # to match (RENAME COLUMN kept it functional, this is hygiene/no-drift).
    op.execute("ALTER INDEX uq_issuer_codice_destinatario_ricezione RENAME TO uq_issuer_sdi_code")
    # The CHECK on the renamed verdict column keeps working (PG rewrote its
    # expression on the column rename); align its name too. The stored name
    # carries the metadata naming-convention prefix (ck_<table>_<name>).
    op.execute(
        "ALTER TABLE received_invoices RENAME CONSTRAINT "
        "ck_received_invoices_committente_verdict_chk "
        "TO ck_received_invoices_buyer_verdict_chk"
    )
    op.execute(_RESOLVER_NEW)
    # Normalize a redundant leading country prefix in vat_number (e.g.
    # 'IT01112223334' with country_code 'IT' -> '01112223334'). Pure data
    # cleanup; the bare IdCodice is what FatturaPA / SdI expect.
    for table in ("client_profile", "issuer_profiles"):
        op.execute(
            f"UPDATE {table} SET vat_number = substring(vat_number from 3) "
            f"WHERE vat_number IS NOT NULL "
            f"AND country_code IS NOT NULL "
            f"AND upper(substring(vat_number from 1 for 2)) = upper(country_code) "
            f"AND substring(vat_number from 3 for 1) ~ '^[0-9]$'"
        )


def downgrade() -> None:
    # Reverse the renames (the vat_number prefix normalization is not restored:
    # the bare value is the correct one and re-adding a prefix would be wrong).
    # Order matters: restore the columns first, then the index name, then the
    # resolver -- its SQL body is validated at CREATE time and references the
    # old column, which must exist again before the function is rebuilt.
    for table, old, new in reversed(_RENAMES):
        op.alter_column(table, new, new_column_name=old)
    op.execute(
        "ALTER TABLE received_invoices RENAME CONSTRAINT "
        "ck_received_invoices_buyer_verdict_chk "
        "TO ck_received_invoices_committente_verdict_chk"
    )
    op.execute("ALTER INDEX uq_issuer_sdi_code RENAME TO uq_issuer_codice_destinatario_ricezione")
    op.execute(_RESOLVER_OLD)
