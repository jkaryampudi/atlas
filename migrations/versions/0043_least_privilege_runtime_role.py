"""F-019/F-020: provision the least-privilege runtime role (atlas_app).

The audit append-only + monotonic-anchor triggers (0042) are enforced by the DB,
but a SUPERUSER runtime bypasses every row trigger via
``SET session_replication_role = 'replica'`` (reproduced: the owner ``atlas``
succeeds; a non-superuser role is refused). Production must therefore connect as a
NON-superuser, NON-owner role. This migration creates that role and grants it
exactly what the app needs — no ownership, no superuser, so it can neither disable
triggers nor set session_replication_role.

Grants (over every non-system schema, discovered dynamically so a new schema is
covered without editing this migration):
  * USAGE on the schema; SELECT/INSERT/UPDATE/DELETE on its tables; USAGE/SELECT on
    its sequences; and the same as DEFAULT privileges for future objects.
  * Belt-and-braces on the audit wall (on top of the triggers): only SELECT+INSERT
    on audit.decision_events (no UPDATE/DELETE), and no DELETE on audit.chain_head.

The role is created LOGIN with a documented LOCAL-ONLY password; production resets
it (``ALTER ROLE atlas_app PASSWORD ...``) out of band and points
ATLAS_DATABASE_URL + ATLAS_DB_REQUIRE_LEAST_PRIVILEGE=true at it. Roles are
cluster-global, so creation is guarded to stay idempotent across the atlas /
atlas_test databases. See ops/sql/provision_runtime_role.sql for the standalone
provisioning form.
"""
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

RUNTIME_ROLE = "atlas_app"
_LOCAL_PASSWORD = "atlas_app_local_only"   # documented throwaway; prod resets it


def upgrade() -> None:
    op.execute(f"""
    DO $$
    DECLARE
      sch text;
    BEGIN
      -- roles are cluster-global: create only if absent (idempotent across DBs)
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RUNTIME_ROLE}') THEN
        CREATE ROLE {RUNTIME_ROLE} LOGIN PASSWORD '{_LOCAL_PASSWORD}'
          NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
      END IF;

      EXECUTE format('GRANT CONNECT ON DATABASE %I TO {RUNTIME_ROLE}',
                     current_database());

      FOR sch IN
        SELECT nspname FROM pg_namespace
        WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'
      LOOP
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO {RUNTIME_ROLE}', sch);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES '
                       'IN SCHEMA %I TO {RUNTIME_ROLE}', sch);
        EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I '
                       'TO {RUNTIME_ROLE}', sch);
        EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
                       'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES '
                       'TO {RUNTIME_ROLE}', sch);
        EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
                       'GRANT USAGE, SELECT ON SEQUENCES TO {RUNTIME_ROLE}', sch);
      END LOOP;
    END $$;

    -- Belt-and-braces on the audit wall — no UPDATE/DELETE on the immutable log,
    -- no DELETE on the anchor, ON TOP OF the 0042 triggers.
    REVOKE UPDATE, DELETE ON audit.decision_events FROM {RUNTIME_ROLE};
    REVOKE DELETE ON audit.chain_head FROM {RUNTIME_ROLE};
    """)


def downgrade() -> None:
    op.execute(f"""
    DO $$
    DECLARE
      sch text;
    BEGIN
      FOR sch IN
        SELECT nspname FROM pg_namespace
        WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'
      LOOP
        EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
                       'REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES '
                       'FROM {RUNTIME_ROLE}', sch);
        EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
                       'REVOKE USAGE, SELECT ON SEQUENCES FROM {RUNTIME_ROLE}', sch);
        EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I '
                       'FROM {RUNTIME_ROLE}', sch);
        EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I '
                       'FROM {RUNTIME_ROLE}', sch);
        EXECUTE format('REVOKE USAGE ON SCHEMA %I FROM {RUNTIME_ROLE}', sch);
      END LOOP;
    END $$;
    """)
    # The role itself is left in place (cluster-global; other DBs may use it).
