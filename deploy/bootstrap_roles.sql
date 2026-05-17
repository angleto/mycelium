-- Bootstrap del ruolo runtime. Idempotente. Eseguito come superuser
-- (flow) PRIMA delle migrazioni applicative. Tiene la password fuori
-- dal version control: arriva da una variabile psql (:app_pw) fornita
-- dall'ambiente. Vedi docs/adr/0015 e il target `make db-bootstrap`.

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'flow_app') THEN
    CREATE ROLE flow_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
END $$;

ALTER ROLE flow_app WITH PASSWORD :'app_pw';
