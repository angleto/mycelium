-- Marks a database as one the test suite may destroy. Idempotent.
--
-- WHY THIS IS ITS OWN FILE, and not two lines appended to
-- bootstrap_roles.sql, which is the obvious place and is the wrong one:
-- that file is also run against the LOCAL DEVELOPMENT database by
-- `make db-bootstrap`. A marker created there would mark the development
-- database too, and the guard would then wave through the exact target it
-- exists to refuse. The marker is only meaningful if the gesture that
-- creates it is one nobody performs by accident, so it lives in a file
-- whose only callers are `make test-db-up` and the CI job.
--
-- Run as the owner role, after the migrations (it is a table like any
-- other and does not need to exist before them).

CREATE TABLE IF NOT EXISTS _mycelium_test_database (
  marked_at timestamptz NOT NULL DEFAULT now(),
  note text NOT NULL
);

-- A single row, so a reader who finds the table also finds out what it is
-- for. Guarded rather than ON CONFLICT: the table has no key, because a
-- key on a one-row table is ceremony.
INSERT INTO _mycelium_test_database (note)
SELECT 'The mycelium test suite may DROP, TRUNCATE and rewrite anything in '
       'this database. Never create this table in a database holding data '
       'anyone would miss.'
WHERE NOT EXISTS (SELECT 1 FROM _mycelium_test_database);
