-- F-019/F-020: provision the least-privilege runtime role (atlas_app).
--
-- The audit append-only + monotonic-anchor triggers (migration 0042) are enforced
-- by the database, but a PostgreSQL SUPERUSER bypasses every row trigger with
--     SET session_replication_role = 'replica';
-- so the wall is only real when the RUNTIME connects as a NON-superuser, NON-owner
-- role. Migration 0043 provisions this same role automatically; this standalone
-- script is for a DBA provisioning a fresh production cluster by hand.
--
-- Run as the database OWNER (the role that owns the schemas/tables). Then:
--   1) ALTER ROLE atlas_app PASSWORD '<a real secret>';        -- replace the local default
--   2) point ATLAS_DATABASE_URL at atlas_app and set
--      ATLAS_DB_REQUIRE_LEAST_PRIVILEGE=true so the app fails closed if it ever
--      finds itself connected as a superuser.
--
-- Idempotent: safe to re-run. Roles are cluster-global; run the GRANT section
-- once per database.

DO $$
DECLARE
  sch text;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'atlas_app') THEN
    CREATE ROLE atlas_app LOGIN PASSWORD 'atlas_app_local_only'
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;

  EXECUTE format('GRANT CONNECT ON DATABASE %I TO atlas_app', current_database());

  FOR sch IN
    SELECT nspname FROM pg_namespace
    WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'
  LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO atlas_app', sch);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO atlas_app', sch);
    EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO atlas_app', sch);
    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO atlas_app', sch);
    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT USAGE, SELECT ON SEQUENCES TO atlas_app', sch);
  END LOOP;
END $$;

-- Belt-and-braces on the audit wall (on top of the 0042 triggers): the runtime
-- role gets no UPDATE/DELETE on the immutable log, and no DELETE on the anchor.
REVOKE UPDATE, DELETE ON audit.decision_events FROM atlas_app;
REVOKE DELETE ON audit.chain_head FROM atlas_app;
