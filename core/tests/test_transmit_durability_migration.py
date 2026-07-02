"""Migration 0079 structure gate (ADR-0046, task b6a0df8f).

Asserts the two-phase-transmit DDL is really in place: the dispatch-lease and
environment columns, the partial index on the NomeFile correlation key, and
the SECURITY DEFINER filename resolver (with the draft exclusion and the
explicit app grant that survives harden_function_acls)."""

from __future__ import annotations

import sqlalchemy as sa

from mycelium_core.config import get_settings


def _engine() -> sa.Engine:
    return sa.create_engine(get_settings().database_url_sync)


def test_dispatch_lease_and_env_columns() -> None:
    engine = _engine()
    try:
        with engine.connect() as conn:
            cols = set(
                conn.execute(
                    sa.text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'invoices' AND column_name IN "
                        "('sdi_dispatch_started_at','sdi_env_used','sdi_resent_at')"
                    )
                ).scalars()
            )
            assert cols == {"sdi_dispatch_started_at", "sdi_env_used", "sdi_resent_at"}
    finally:
        engine.dispose()


def test_nome_file_partial_index() -> None:
    engine = _engine()
    try:
        with engine.connect() as conn:
            idxdef = conn.execute(
                sa.text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_invoices_nome_file'")
            ).scalar_one()
            assert "nome_file IS NOT NULL" in idxdef
    finally:
        engine.dispose()


def test_filename_resolver_function() -> None:
    engine = _engine()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT prosecdef, pg_get_functiondef(p.oid) "
                    "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = 'public' "
                    "AND p.proname = 'sdi_resolve_invoice_org_by_filename'"
                )
            ).one()
            assert row[0] is True  # SECURITY DEFINER
            body = row[1]
            assert "search_path" in body  # pinned search path
            assert "state <> 'draft'" in body  # drafts never adopt notifications
            grant = conn.execute(
                sa.text(
                    "SELECT has_function_privilege("
                    "'mycelium_app', "
                    "'public.sdi_resolve_invoice_org_by_filename(text)', 'EXECUTE')"
                )
            ).scalar_one()
            assert grant is True
    finally:
        engine.dispose()
