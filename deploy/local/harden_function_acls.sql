-- Reproduce the production function-execute posture. Idempotent. Run as
-- the owner role (mycelium) AFTER `alembic upgrade head` (the functions
-- must already exist). See deploy/local/bootstrap_roles.sql (role) and
-- docs/adr/0015 (two-role RLS).
--
-- WHY THIS EXISTS
-- ADR-0015 gives the runtime role `mycelium_app` only the privileges it is
-- explicitly granted. PostgreSQL, however, grants EXECUTE on every new
-- function to PUBLIC by default, and `mycelium_app` is a member of PUBLIC.
-- In production the default PUBLIC execute is revoked (a hardening step
-- outside the migrations), so the app role can call ONLY the functions a
-- migration explicitly `GRANT EXECUTE ... TO mycelium_app`. A fresh dev/CI
-- database, built straight from the migrations, keeps the default PUBLIC
-- execute -- which silently masks a missing grant: a function the app calls
-- directly works in dev/CI and 500s in prod with `permission denied for
-- function ...` (the /advisory/what-now -> tasks_event_end incident,
-- migration 0059). Running this after migrations makes dev/CI reproduce the
-- prod contract, so that class of bug fails a test instead of reaching prod.
--
-- SCOPE: only OUR functions (defined by our migrations). Extension-owned
-- functions (pgvector, pg_trgm, btree_gist -- 300+ of them) are left with
-- their default PUBLIC execute: the app uses them via operators (the vector
-- distance and trigram match operators) and never gets an explicit grant for
-- them, matching prod (where search works). They are identified by a
-- pg_depend extension membership row (deptype 'e') and skipped.
--
-- NB: kept free of the percent character on purpose -- this file is executed
-- both via `psql -f` and via a pyformat DBAPI driver (the pytest fixture),
-- where a literal percent would be parsed as a parameter placeholder.
--
-- This revokes a redundant grant only; every function the app legitimately
-- calls directly already has an explicit `GRANT EXECUTE ... TO mycelium_app`
-- (the SECURITY DEFINER RPCs, the SDI resolvers, tasks_event_end). The
-- trigger / SECURITY-DEFINER-internal functions need no direct grant: a
-- trigger fires in the engine's context, not the calling role's, and an
-- internal helper is reached through its SECURITY DEFINER caller.

DO $$
DECLARE
  r record;
BEGIN
  FOR r IN
    SELECT p.oid::regprocedure AS sig
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public'
      AND p.prokind = 'f'  -- plain functions only (not aggregates/procedures)
      AND NOT EXISTS (
        SELECT 1 FROM pg_depend d
        WHERE d.objid = p.oid AND d.deptype = 'e'  -- skip extension-owned
      )
  LOOP
    EXECUTE 'REVOKE EXECUTE ON FUNCTION ' || r.sig::text || ' FROM PUBLIC';
  END LOOP;
END $$;
