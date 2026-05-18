-- Bootstrap of the runtime role. Idempotent. Run as superuser
-- (flow) BEFORE the application migrations. Keeps the password out of
-- version control: it comes from a psql variable (:app_pw) provided by
-- the environment. See docs/adr/0015 and the `make db-bootstrap` target.

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'flow_app') THEN
    CREATE ROLE flow_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
END $$;

ALTER ROLE flow_app WITH PASSWORD :'app_pw';
