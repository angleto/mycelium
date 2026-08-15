"""Migration 0092 structure gate (inbound payment connectors, ADR-0051).

Asserts the DATABASE posture the subsystem rests on, read back from a live
database rather than from the migration source: a migration that ran is the
only evidence that counts, and several of these guarantees (an RLS mode, a
function ACL, a FK action) are invisible in the ORM models.

Covers:

- the deliberate RLS ASYMMETRY -- ``payment_connectors`` is ENABLE-but-NOT-FORCE
  while its four siblings are ENABLE **and** FORCE -- plus the org policy and
  the app-role grants on all five tables;
- ``public.resolve_payment_connector``: SECURITY DEFINER, pinned search_path,
  the exact OUT-parameter contract, ``SETOF record`` (so an unknown connector
  yields NO row), PUBLIC's EXECUTE revoked and ``mycelium_app``'s granted;
- the four UNIQUE constraints that carry the idempotency guarantees, by their
  exact ordered column list;
- the three partial indexes, WITH their predicates (a full index would still
  "exist" and would silently change what the drain and the security view cost);
- the closed vocabularies: the CHECK constraints must admit EXACTLY the values
  in the Python tuples of ``models.payment_connector``. This is the drift guard
  -- it is what stops the model and the schema diverging, in either direction;
- both actor-kind CHECKs (including the historically DOUBLED
  ``ck_entity_revision_ck_entity_revision_actor_kind``) admitting
  ``payment_connector`` without losing the kinds that were there before;
- ``payment_object_links.invoice_id`` being ON DELETE RESTRICT.

Runs on the sync (owner) engine, like ``test_migrations.py`` and
``test_issuer_api_key_migration.py``; assumes the test DB is at
``alembic upgrade head``.

NOTE on names: the ``ck_%(table_name)s_%(constraint_name)s`` naming convention
prefixes an explicitly named CHECK a second time, so the constraints below are
stored as ``ck_payment_connectors_ck_payment_connectors_provider`` and, past 63
characters, as a truncated+hashed name. CHECKs are therefore located by the
COLUMN they constrain, never by name; the UNIQUE constraints and the indexes
keep the names the migration gave them and are looked up by name.
"""

from __future__ import annotations

import os
import re

import pytest
import sqlalchemy as sa

from mycelium_core.models.payment_connector import (
    AUTOMATION_MODES,
    DELIVERY_OUTCOMES,
    EMISSION_EVENTS,
    EVENT_STATUSES,
    OBJECT_KINDS,
    PROVIDERS,
)

#: The tables that FORCE row level security: nothing, not even the owning role,
#: reads them without a tenant GUC.
FORCED_TABLES = (
    "payment_connector_events",
    "payment_object_links",
    "payment_customer_links",
    "payment_webhook_deliveries",
)
ALL_TABLES = ("payment_connectors", *FORCED_TABLES)

#: ``(table, column, python vocabulary)``. The CHECK on that column must admit
#: exactly this set -- no more (the DB would accept a value no code can produce)
#: and no less (the code would produce a value the DB refuses, i.e. a 500 on a
#: fiscal write path).
VOCABULARIES = (
    ("payment_connectors", "provider", PROVIDERS),
    ("payment_connectors", "invoice_mode", AUTOMATION_MODES),
    ("payment_connectors", "credit_note_mode", AUTOMATION_MODES),
    ("payment_connectors", "emission_event", EMISSION_EVENTS),
    ("payment_connector_events", "status", EVENT_STATUSES),
    ("payment_object_links", "object_kind", OBJECT_KINDS),
    ("payment_webhook_deliveries", "outcome", DELIVERY_OUTCOMES),
)

#: Every quoted literal that is immediately cast -- ``'stripe'::character
#: varying`` inside the rendered ``= ANY (ARRAY[...])``. The cast is what
#: distinguishes a vocabulary member from an incidental string in the
#: expression, and ``(provider)::text`` on the left-hand side is not matched
#: because it carries no quotes.
_LITERAL = re.compile(r"'([^']*)'::")


def _engine() -> sa.Engine:
    sync_url = os.environ.get("MYCELIUM_DATABASE_URL_SYNC")
    if not sync_url:
        pytest.skip("MYCELIUM_DATABASE_URL_SYNC not set")
    return sa.create_engine(sync_url, future=True)


def _column_check(conn: sa.Connection, table: str, column: str) -> str:
    """The single CHECK constraint on ``table.column``, rendered.

    Located through ``conkey`` rather than by name: see the module docstring on
    the doubled/truncated CHECK names. ``scalar_one`` is deliberate -- a second
    CHECK on the same column would mean two vocabularies are in play and the
    drift guard below would only be reading one of them.
    """
    return str(
        conn.execute(
            sa.text(
                "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) "
                "WHERE c.conrelid = CAST(:t AS regclass) AND c.contype = 'c' AND a.attname = :col"
            ),
            {"t": table, "col": column},
        ).scalar_one()
    )


def _constraint_columns(conn: sa.Connection, conname: str) -> tuple[str, list[str]]:
    """``(contype, ordered column names)`` for a named constraint.

    The ORDER matters as much as the membership: a unique constraint is an
    index, and ``(connector_id, provider_event_id)`` reversed would still
    dedupe but would stop serving the per-connector lookups.
    """
    row = conn.execute(
        sa.text(
            "SELECT c.contype, array_agg(a.attname ORDER BY k.ord) AS cols "
            "FROM pg_constraint c "
            "JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
            "WHERE c.conname = :n GROUP BY c.contype"
        ),
        {"n": conname},
    ).one()
    return str(row.contype), list(row.cols)


def _index(conn: sa.Connection, indexname: str) -> tuple[str | None, str]:
    """``(predicate, full definition)`` for an index. Predicate is None when the
    index is NOT partial."""
    row = conn.execute(
        sa.text(
            "SELECT pg_get_expr(x.indpred, x.indrelid) AS pred, "
            "       pg_get_indexdef(x.indexrelid) AS idxdef "
            "FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
            "WHERE i.relname = :n"
        ),
        {"n": indexname},
    ).one()
    return (None if row.pred is None else str(row.pred)), str(row.idxdef)


# --- row level security ----------------------------------------------------


def test_payment_connectors_is_enable_but_not_force_rls() -> None:
    """The one table in the subsystem that must NOT force RLS.

    An inbound provider webhook arrives with no session and no bearer, so
    ``app.current_org`` does not exist yet and the tenant has to be resolved
    BEFORE any tenant context does. That resolution runs through the owner-run
    SECURITY DEFINER ``resolve_payment_connector``. FORCE ROW LEVEL SECURITY
    applies the policy to the table's OWNER too, and the policy compares
    ``org_id`` against an unset GUC (NULL), which is never true -- so under
    FORCE the resolver would see zero rows and EVERY inbound webhook would 404
    with the connector sitting right there. ENABLE alone keeps the policy on
    for the app role (which is what confines a tenant to its own connectors)
    while leaving the resolver able to do its one job.
    """
    engine = _engine()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = 'payment_connectors'"
                )
            ).one()
            assert row.relrowsecurity is True, "RLS off would expose connectors cross-tenant"
            assert row.relforcerowsecurity is False, (
                "FORCE would blind the SECURITY DEFINER resolver and 404 every webhook"
            )
    finally:
        engine.dispose()


def test_sibling_tables_force_row_level_security() -> None:
    """Everything the connector WRITES is FORCE: those tables are only ever
    touched from a ``tenant_session``, so there is no reader to exempt, and the
    payload/ledger rows are the most sensitive in the subsystem (a raw provider
    event carries the counterpart's personal and fiscal data)."""
    engine = _engine()
    try:
        with engine.connect() as conn:
            for table in FORCED_TABLES:
                row = conn.execute(
                    sa.text(
                        "SELECT relrowsecurity, relforcerowsecurity "
                        "FROM pg_class WHERE relname = :t"
                    ),
                    {"t": table},
                ).one()
                assert row.relrowsecurity is True, f"{table}: RLS not enabled"
                assert row.relforcerowsecurity is True, f"{table}: RLS not forced"
    finally:
        engine.dispose()


def test_every_table_carries_its_org_policy() -> None:
    """All five tables have ``p_<table>``, scoped on the org GUC, for ALL
    commands.

    Enabling RLS without a policy is fail-closed (the app role would see
    nothing), so the policy's PRESENCE is an availability guarantee; its
    PREDICATE is the tenancy guarantee. Both halves are asserted: a policy whose
    WITH CHECK drifted away from its USING would let a tenant write rows
    stamped with somebody else's ``org_id``.
    """
    engine = _engine()
    try:
        with engine.connect() as conn:
            for table in ALL_TABLES:
                row = conn.execute(
                    sa.text(
                        "SELECT policyname, cmd, qual, with_check FROM pg_policies "
                        "WHERE tablename = :t"
                    ),
                    {"t": table},
                ).one()
                assert row.policyname == f"p_{table}"
                assert row.cmd == "ALL", f"{table}: policy does not cover writes"
                for expression in (row.qual, row.with_check):
                    assert expression is not None, f"{table}: half of the policy is missing"
                    assert "org_id" in expression, (
                        f"{table}: policy is not org-scoped: {expression}"
                    )
                    assert "app.current_org" in expression, (
                        f"{table}: policy does not read the tenant GUC: {expression}"
                    )
    finally:
        engine.dispose()


def test_app_role_can_read_and_write_every_table() -> None:
    """ADR-0015's second half: RLS confines the runtime role, the GRANTs are
    what let it work at all. A missing grant here is a 500 on the ingress that
    no amount of correct policy would explain."""
    engine = _engine()
    try:
        with engine.connect() as conn:
            for table in ALL_TABLES:
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    granted = conn.execute(
                        sa.text("SELECT has_table_privilege('mycelium_app', :t, :p)"),
                        {"t": table, "p": privilege},
                    ).scalar_one()
                    assert granted is True, f"mycelium_app lacks {privilege} on {table}"
    finally:
        engine.dispose()


# --- the tenant resolver ---------------------------------------------------


def test_resolver_is_security_definer_with_a_pinned_search_path() -> None:
    """The resolver's whole reason to exist is running as its owner, and a
    SECURITY DEFINER function without a pinned ``search_path`` is the classic
    escalation: anyone able to set the search_path could shadow a referenced
    object and have it executed with the owner's rights."""
    engine = _engine()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT prosecdef, proconfig::text AS cfg, "
                    "       pg_get_function_result(oid) AS result, "
                    "       pg_get_function_arguments(oid) AS args "
                    "FROM pg_proc WHERE proname = 'resolve_payment_connector'"
                )
            ).one()
            assert row.prosecdef is True, "the resolver must run as its owner"
            assert "search_path=" in row.cfg, f"search_path not pinned: {row.cfg}"
            # SETOF, so "no such connector" (and "revoked") is ZERO rows rather
            # than one row of NULLs -- the ingress reads the row count.
            assert row.result == "SETOF record", row.result
            # The OUT contract, types included: the two ciphertexts are text
            # (Fernet envelopes, decrypted back) and the two key hashes are
            # bytea (compared, never decrypted). Swapping those is a runtime
            # TypeError on the unauthenticated path.
            assert row.args.startswith("p_connector_id uuid"), row.args
            for out in (
                "OUT out_org_id uuid",
                "OUT out_issuer_profile_id uuid",
                "OUT out_provider text",
                "OUT out_enabled boolean",
                "OUT out_signing_secret_ciphertext text",
                "OUT out_previous_signing_secret_ciphertext text",
                "OUT out_api_key_hash bytea",
                "OUT out_previous_api_key_hash bytea",
            ):
                assert out in row.args, f"missing {out} in {row.args}"
    finally:
        engine.dispose()


def test_resolver_execute_is_revoked_from_public_and_granted_to_app() -> None:
    """A SECURITY DEFINER function that PUBLIC may execute is a credential
    oracle: it returns a connector's signing-secret envelope and key hashes for
    any uuid, with no tenant context by construction. ``proacl`` must therefore
    be non-default (a NULL acl means "PUBLIC has EXECUTE") and must name
    ``mycelium_app`` explicitly, since the runtime role is a member of PUBLIC
    and would otherwise lose the ability to call it once PUBLIC is revoked.
    """
    engine = _engine()
    try:
        with engine.connect() as conn:
            raw_acl = conn.execute(
                sa.text(
                    "SELECT proacl::text FROM pg_proc "
                    "WHERE oid = CAST('public.resolve_payment_connector(uuid)' AS regprocedure)"
                )
            ).scalar_one()
            assert raw_acl is not None, "default (PUBLIC-executable) ACL on a SECURITY DEFINER fn"
            # grantee 0 is PUBLIC and renders as '-' through regrole.
            grants = {
                (str(g), str(p))
                for g, p in conn.execute(
                    sa.text(
                        "SELECT coalesce(nullif(CAST(a.grantee AS regrole)::text, '-'), 'PUBLIC'), "
                        "       a.privilege_type "
                        "FROM pg_proc p, aclexplode(p.proacl) a "
                        "WHERE p.oid = "
                        "CAST('public.resolve_payment_connector(uuid)' AS regprocedure)"
                    )
                ).all()
            }
            assert ("PUBLIC", "EXECUTE") not in grants, f"PUBLIC can execute the resolver: {grants}"
            assert ("mycelium_app", "EXECUTE") in grants, f"the app role cannot resolve: {grants}"
    finally:
        engine.dispose()


def test_resolver_answers_an_unknown_connector_with_no_row() -> None:
    """Behavioural companion to the signature check: an id nobody ever minted
    produces zero rows, not a row of NULLs. The ingress turns "no row" into the
    404 it also gives a revoked connector, so the surface is not an oracle for
    which uuids exist."""
    engine = _engine()
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT * FROM public.resolve_payment_connector(CAST(:cid AS uuid))"),
                {"cid": "00000000-0000-0000-0000-000000000000"},
            ).all()
            assert rows == []
    finally:
        engine.dispose()


# --- idempotency claims ----------------------------------------------------


def test_idempotency_unique_constraints_carry_the_right_columns() -> None:
    """The four UNIQUE constraints ARE the correctness argument of this
    subsystem; ``ON CONFLICT DO NOTHING`` is only as good as the index behind
    it.

    - events ``(connector_id, provider_event_id)``: a provider redelivery, a
      client retry and two replicas racing one POST collapse onto one row;
    - object links ``(connector_id, object_kind, object_id)``: one provider
      object can name exactly one emitted document -- this is what stands
      between a redelivery and a second fiscal number for the same charge;
    - customer links ``(connector_id, provider_customer_id)``: closes the
      SELECT-then-INSERT race in ``resolve_or_create_client`` that would
      otherwise yield two client tags (and two sezionali) for one customer;
    - connectors ``(issuer_profile_id, label)``: the natural key that lets a
      live and a test account coexist on one cedente.

    Scoped on the CONNECTOR, never global: two connectors in one org legitimately
    see the same ``in_1`` from two different Stripe accounts.
    """
    engine = _engine()
    try:
        with engine.connect() as conn:
            for conname, expected in (
                ("uq_payment_connector_events_dedupe", ["connector_id", "provider_event_id"]),
                (
                    # ``dry_run`` is part of the key (migration 0093): a shadow
                    # claim and a live claim for the same provider object must
                    # coexist, and the discriminator has to be a column rather
                    # than a prefix on object_id -- on the native contract that
                    # id is the SENDER's own reference, so a reserved string
                    # form would let a sender make a live run resolve to a
                    # shadow document and file it.
                    "uq_payment_object_links_object",
                    ["connector_id", "object_kind", "object_id", "dry_run"],
                ),
                (
                    "uq_payment_customer_links_customer",
                    ["connector_id", "provider_customer_id"],
                ),
                ("uq_payment_connectors_label", ["issuer_profile_id", "label"]),
            ):
                contype, columns = _constraint_columns(conn, conname)
                assert contype == "u", f"{conname} is not a UNIQUE constraint (contype={contype})"
                assert columns == expected, f"{conname} covers {columns}, expected {expected}"
    finally:
        engine.dispose()


def test_partial_indexes_carry_their_predicates() -> None:
    """The three predicates the runtime depends on.

    A non-partial index of the same columns would satisfy "the index exists"
    and quietly change the cost model: ``_due`` and ``_processing`` are the
    drain and lease-reclaim queries, which run every tick over a table that
    grows with every webhook ever received, and ``_refused`` is the security
    view over a ledger whose overwhelming majority is ``accepted``. So the
    predicate is asserted, not just the name.
    """
    engine = _engine()
    try:
        with engine.connect() as conn:
            due, due_def = _index(conn, "ix_payment_connector_events_due")
            assert due is not None, "the drain index is not partial"
            assert "status" in due and "'pending'" in due, due
            assert "next_attempt_at" in due_def, due_def

            processing, processing_def = _index(conn, "ix_payment_connector_events_processing")
            assert processing is not None, "the lease-reclaim index is not partial"
            assert "status" in processing and "'processing'" in processing, processing
            assert "last_attempt_at" in processing_def, processing_def

            refused, refused_def = _index(conn, "ix_payment_webhook_deliveries_refused")
            assert refused is not None, "the refusal index is not partial"
            # Postgres renders ``NOT IN (a, b)`` as ``<> ALL (ARRAY[a, b])``.
            # The NEGATION is the load-bearing part: an index over the accepted
            # deliveries instead of the refused ones would index the whole table
            # and answer the wrong question.
            assert "<> ALL" in refused or "NOT IN" in refused, refused
            assert "'accepted'" in refused and "'duplicate'" in refused, refused
            assert "outcome" in refused, refused
            assert "connector_id" in refused_def and "received_at" in refused_def, refused_def
    finally:
        engine.dispose()


# --- closed vocabularies (drift guard) -------------------------------------


def test_check_constraints_match_the_python_vocabularies_exactly() -> None:
    """The schema and ``models.payment_connector`` must agree, both ways.

    Python -> DB: a value the code can produce but the CHECK refuses is a
    check_violation in the middle of a fiscal write -- the ingress 500s and the
    provider redelivers forever.

    DB -> Python: a value the CHECK admits but no tuple lists is a vocabulary
    the SPA never renders and the runner never dispatches on (the connector
    router serves these tuples verbatim to the frontend), so a row can end up in
    a state nothing knows how to display or resume.

    Widening a vocabulary is therefore a two-file change by construction, and
    this test is what makes forgetting either half fail in CI instead of in
    production.
    """
    engine = _engine()
    try:
        with engine.connect() as conn:
            for table, column, vocabulary in VOCABULARIES:
                cdef = _column_check(conn, table, column)
                admitted = set(_LITERAL.findall(cdef))
                for value in vocabulary:
                    assert value in admitted, f"{table}.{column} refuses {value!r}: {cdef}"
                assert admitted == set(vocabulary), (
                    f"{table}.{column} drifted from the Python tuple: "
                    f"DB={sorted(admitted)} Python={sorted(vocabulary)}"
                )
    finally:
        engine.dispose()


def test_actor_kind_checks_admit_payment_connector() -> None:
    """Both audit CHECKs widened, or every audited write from the connector
    raises a check_violation.

    The connector is the ACTOR of the documents it emits (``tenant_session(...,
    actor_kind="payment_connector")`` in the ingress, the worker and the
    runner), so ``audit.log`` and the entity-revision trigger both stamp that
    kind. A missing widening would not be a cosmetic audit gap: it would fail
    the transaction that files the invoice.

    ``entity_revision``'s constraint carries the historically doubled name (0006
    passed an explicit ``name=`` that the ``ck_%(table_name)s_%(constraint_name)s``
    convention prefixed again); ``activity_log``'s is clean.
    """
    engine = _engine()
    try:
        with engine.connect() as conn:
            for conname in (
                "ck_activity_log_actor_kind",
                "ck_entity_revision_ck_entity_revision_actor_kind",
            ):
                row = conn.execute(
                    sa.text(
                        "SELECT pg_get_constraintdef(oid) AS cdef, convalidated "
                        "FROM pg_constraint WHERE conname = :n"
                    ),
                    {"n": conname},
                ).one()
                assert "payment_connector" in row.cdef, f"{conname} not widened: {row.cdef}"
                # A widening, not a replacement: the kinds that were already
                # there must survive, including 0077's.
                for previous in ("issuer_api_key", "mcp_token", "system", "human_direct"):
                    assert previous in row.cdef, f"{conname} lost {previous}: {row.cdef}"
                # NOT VALID is the documented DOWNGRADE shape (append-only audit
                # rows cannot be deleted to make a re-narrowing validate). The
                # upgrade must leave a VALIDATED constraint.
                assert row.convalidated is True, f"{conname} is NOT VALID at head"
    finally:
        engine.dispose()


# --- referential actions ---------------------------------------------------


def test_object_link_invoice_fk_is_restrict_and_connector_fk_cascades() -> None:
    """The emission claim must not outlive its invoice, and must not outlive
    the connector either.

    ``payment_object_links.invoice_id`` is ON DELETE RESTRICT: SET NULL would
    leave a claim pointing at nothing, and a dangling claim is worse than no
    claim at all -- the next event for that money would find a link, resolve to
    a NULL invoice and either crash or, on the other branch, emit again. That
    is the double-filing door this table exists to keep shut. NOT NULL on the
    column is the same guarantee stated at the column level.

    ``connector_id`` CASCADEs, which is what makes ``purge_connector`` mean
    "forget which documents came from this connector" while the invoices
    themselves are untouched: the links go, the fiscal record does not.
    """
    engine = _engine()
    try:
        with engine.connect() as conn:
            actions = {
                str(name): str(action)
                for name, action in conn.execute(
                    sa.text(
                        "SELECT a.attname, c.confdeltype FROM pg_constraint c "
                        "JOIN pg_attribute a "
                        "  ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
                        "WHERE c.conrelid = CAST('payment_object_links' AS regclass) "
                        "AND c.contype = 'f'"
                    )
                ).all()
            }
            # 'r' = RESTRICT, 'n' = SET NULL, 'c' = CASCADE, 'a' = NO ACTION.
            assert actions["invoice_id"] == "r", (
                f"invoice_id ON DELETE is {actions['invoice_id']!r}, expected RESTRICT: "
                "a dangling emission claim re-opens double filing"
            )
            assert actions["connector_id"] == "c", actions["connector_id"]
            not_null = conn.execute(
                sa.text(
                    "SELECT attnotnull FROM pg_attribute "
                    "WHERE attrelid = CAST('payment_object_links' AS regclass) "
                    "AND attname = 'invoice_id'"
                )
            ).scalar_one()
            assert not_null is True, "a claim with no invoice is not a claim"
    finally:
        engine.dispose()


# --- migration 0093: shadow mode -------------------------------------------


def test_dry_run_columns_exist_with_the_right_types() -> None:
    """0093's columns. ``dry_run`` must be NOT NULL with a false default, or an
    existing row would be neither shadow nor live and would match neither
    lookup."""
    engine = _engine()
    try:
        with engine.connect() as conn:
            rows = dict(
                conn.execute(
                    sa.text(
                        "SELECT table_name || '.' || column_name, "
                        "       data_type || '/' || is_nullable || '/' "
                        "       || coalesce(column_default, 'none') "
                        "FROM information_schema.columns "
                        "WHERE (table_name, column_name) IN "
                        "  (('payment_connector_events','dry_run'), "
                        "   ('payment_connector_events','dry_run_xml'), "
                        "   ('payment_object_links','dry_run'))"
                    )
                ).all()
            )
        assert rows["payment_connector_events.dry_run"] == "boolean/NO/false"
        assert rows["payment_object_links.dry_run"] == "boolean/NO/false"
        assert rows["payment_connector_events.dry_run_xml"].startswith("text/YES")
    finally:
        engine.dispose()


def test_the_automation_mode_check_admits_dry_run() -> None:
    """The CHECK is the last line of defence on a fiscal switch: a mode the
    database does not know cannot be written by any code path.

    Both constraint names carry a DOUBLED prefix, from 0092 passing an
    already-complete name to ``sa.CheckConstraint`` under the
    ``ck_%(table_name)s_%(constraint_name)s`` convention. The names stand;
    asserting the real ones is what stops a later migration guessing wrong.
    """
    engine = _engine()
    try:
        with engine.connect() as conn:
            for column in ("invoice_mode", "credit_note_mode"):
                name = f"ck_payment_connectors_ck_payment_connectors_{column}"
                definition = conn.execute(
                    sa.text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"
                    ),
                    {"n": name},
                ).scalar_one()
                for mode in AUTOMATION_MODES:
                    assert f"'{mode}'" in definition, f"{name} rejects {mode}"
    finally:
        engine.dispose()
