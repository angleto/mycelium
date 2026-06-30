-- Production rename: database `flow` -> `mycelium`, roles `flow`/`flow_app`
-- -> `mycelium`/`mycelium_app`. One-shot, run by a SUPERUSER during a
-- maintenance window with the application STOPPED.
--
-- Why this is not an Alembic migration: you cannot ALTER DATABASE ... RENAME
-- while connected to that database, and ALTER ROLE ... RENAME needs elevated
-- privilege. Run it out-of-band, connected to another DB (e.g. `postgres`).
--
--   psql "postgresql://<superuser>@<host>:5432/postgres" \
--        -v ON_ERROR_STOP=1 -f deploy/rename-flow-to-mycelium.sql
--
-- RENAME preserves: passwords, grants, table/object ownership, RLS policies,
-- and data. Only the identifiers change. It is reversible by swapping the
-- names back, as long as no new `flow`/`mycelium` objects were created in
-- between.

\set ON_ERROR_STOP on

-- 1. Drop residual connections to the old database so the rename can lock it.
SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
 WHERE datname = 'flow'
   AND pid <> pg_backend_pid();

-- 2. Roles first (order independent; grants/ownership follow the role).
ALTER ROLE flow     RENAME TO mycelium;      -- owner / Alembic (sync) role
ALTER ROLE flow_app RENAME TO mycelium_app;  -- runtime (RLS) role

-- 3. Database.
ALTER DATABASE flow RENAME TO mycelium;

-- After this:
--  * point the app at .../mycelium with roles mycelium / mycelium_app
--    (the MYCELIUM_DATABASE_URL / MYCELIUM_DATABASE_URL_SYNC env vars);
--  * role PASSWORDS are unchanged — only the env var NAMES moved from the
--    FLOW_ prefix to MYCELIUM_, the secret VALUES stay the same;
--  * no Alembic migration runs as part of this; the schema is untouched.
