"""F-019/F-020 hardening: audit triggers ENABLE ALWAYS (replica-mode proof).

The 0042 append-only + monotonic-anchor triggers were created with the default
enablement (``tgenabled='O'`` — origin only). A PostgreSQL SUPERUSER can then set
``session_replication_role='replica'`` to suppress every origin trigger and
UPDATE/DELETE audit rows or rewrite the anchor — the exact bypass an independent
review reproduced end-to-end (as the owner ``atlas``: normal UPDATE refused, but
after ``SET session_replication_role='replica'`` UPDATE→'TAMPERED' and full DELETE
both succeeded).

``ENABLE ALWAYS`` makes a trigger fire REGARDLESS of ``session_replication_role``,
so replica mode can no longer silence the audit wall — the invariant-4 guarantee
holds even against a superuser runtime, independent of which role connects. This
is the DB-layer belt-and-braces that makes the wall real rather than opt-in.

(The complementary least-privilege runtime role — atlas_app, migration 0043 — still
prevents the OVERT bypass a superuser retains: DROP/DISABLE TRIGGER. ENABLE ALWAYS
closes the SUBTLE replica-mode bypass that needs no DDL; the non-superuser runtime
closes the overt one. Both are now in place.)

Also tightens 0043: future audit tables must NOT be UPDATE/DELETE-able by the
runtime role (0043's schema-wide DEFAULT PRIVILEGES granted UPDATE/DELETE to
atlas_app; the belt-and-braces REVOKE covered only the two audit tables that
existed then). We revoke UPDATE/DELETE from the audit schema's DEFAULT privileges
so a later audit table inherits SELECT+INSERT only.
"""
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

RUNTIME_ROLE = "atlas_app"


def upgrade() -> None:
    op.execute(f"""
    -- fire even under session_replication_role='replica' (superuser bypass proof)
    ALTER TABLE audit.decision_events
      ENABLE ALWAYS TRIGGER decision_events_append_only;
    ALTER TABLE audit.chain_head
      ENABLE ALWAYS TRIGGER chain_head_guard;

    -- future audit tables: runtime role gets SELECT+INSERT only, never UPDATE/DELETE
    ALTER DEFAULT PRIVILEGES IN SCHEMA audit
      REVOKE UPDATE, DELETE ON TABLES FROM {RUNTIME_ROLE};
    """)


def downgrade() -> None:
    op.execute(f"""
    ALTER TABLE audit.decision_events
      ENABLE TRIGGER decision_events_append_only;   -- back to origin-only ('O')
    ALTER TABLE audit.chain_head
      ENABLE TRIGGER chain_head_guard;
    ALTER DEFAULT PRIVILEGES IN SCHEMA audit
      GRANT UPDATE, DELETE ON TABLES TO {RUNTIME_ROLE};
    """)
