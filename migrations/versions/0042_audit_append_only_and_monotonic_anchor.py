"""F-019 + F-020: enforce audit append-only at the DB, and make the anchor monotonic.

Re-verification found both prior fixes were bypassable:

F-019 — epoch-2 hashing binds identity for NEW events, but LEGACY (hash_version=1)
rows still verify under a formula that omits entity/actor, so tampering their
identity columns was undetectable. Rather than re-hash history, we PREVENT the
tamper: a BEFORE UPDATE/DELETE trigger on audit.decision_events refuses every row
mutation UNCONDITIONALLY (invariant 4 is now enforced by the database, not just by
convention + the app). A dropped/changed identity column can no longer happen on
any row, legacy or new. (TRUNCATE is statement-level and does not fire a row
trigger, so the test harness can still reset the shared DB.)

F-020 — audit.chain_head caught tail truncation only when the anchor was intact,
but (a) PostgresAuditLog recomputed event_count via count(*) on every append, so
deleting the tail then appending one event SELF-HEALED the anchor, and (b) the
chain_head guard only required the atlas.audit_writer GUC, which any DB writer can
set — so the anchor could be rewritten to a truncated terminal. This migration:
  * makes the chain_head guard MONOTONIC — last_seq and event_count may never
    decrease, and the anchor row may never be deleted, REGARDLESS of the GUC — so
    an attacker cannot lower the anchor to match a truncated chain; and
  * (with the append-only trigger above) makes tail/interior DELETE impossible.
PostgresAuditLog is changed in the same commit to advance event_count by a
monotonic +1 rather than count(*), so it never self-heals.

Residual (documented, not closed here): the anchor still lives in the same DB.
A determined table OWNER could TRUNCATE both tables together (a full wipe, not a
stealthy tail truncation — and it needs ownership, not a writer role). Moving the
anchor to an append-only/WORM external store or signing it with a key the DB role
does not hold is the further hardening (a Principal/infra decision).
"""
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    -- F-019 / F-020: audit.decision_events is append-only (invariant 4), enforced
    -- unconditionally at the DB. No GUC bypass — there is NO legitimate UPDATE or
    -- DELETE of an audit row. INSERT is unaffected; TRUNCATE (statement-level) is
    -- not a row trigger and stays available to the disposable test DB.
    CREATE OR REPLACE FUNCTION audit.guard_decision_events_append_only()
      RETURNS trigger AS $$
    BEGIN
      RAISE EXCEPTION
        'audit.decision_events is append-only (invariant 4) — % refused; audit '
        'rows are immutable once written (F-019/F-020)', TG_OP;
    END;
    $$ LANGUAGE plpgsql;

    DROP TRIGGER IF EXISTS decision_events_append_only ON audit.decision_events;
    CREATE TRIGGER decision_events_append_only
      BEFORE UPDATE OR DELETE ON audit.decision_events
      FOR EACH ROW EXECUTE FUNCTION audit.guard_decision_events_append_only();

    -- F-020: the anchor is MONOTONIC and undeletable. The GUC still gates who may
    -- advance it, but the monotonic invariant holds even WITH the GUC set, so an
    -- attacker who sets atlas.audit_writer cannot lower the anchor to a truncated
    -- terminal, and can never delete it.
    CREATE OR REPLACE FUNCTION audit.guard_chain_head() RETURNS trigger AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'audit.chain_head anchor may not be deleted (F-020)';
      END IF;
      IF current_setting('atlas.audit_writer', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'audit.chain_head is protected — only the audit writer '
          'may advance the anchor';
      END IF;
      IF NEW.last_seq < OLD.last_seq OR NEW.event_count < OLD.event_count THEN
        RAISE EXCEPTION 'audit.chain_head is monotonic — last_seq/event_count may '
          'not decrease (tail-truncation / anchor-rewrite attempt, F-020): '
          'seq % -> %, count % -> %',
          OLD.last_seq, NEW.last_seq, OLD.event_count, NEW.event_count;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    -- re-point the existing trigger at the strengthened function (0036 created it
    -- BEFORE UPDATE OR DELETE, which is what we still want).
    DROP TRIGGER IF EXISTS chain_head_guard ON audit.chain_head;
    CREATE TRIGGER chain_head_guard
      BEFORE UPDATE OR DELETE ON audit.chain_head
      FOR EACH ROW EXECUTE FUNCTION audit.guard_chain_head();
    """)


def downgrade() -> None:
    op.execute("""
    DROP TRIGGER IF EXISTS decision_events_append_only ON audit.decision_events;
    DROP FUNCTION IF EXISTS audit.guard_decision_events_append_only();

    -- restore the 0036 (GUC-only, non-monotonic) chain_head guard.
    CREATE OR REPLACE FUNCTION audit.guard_chain_head() RETURNS trigger AS $$
    BEGIN
      IF current_setting('atlas.audit_writer', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'audit.chain_head is protected — only the audit writer '
          'may write it';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    DROP TRIGGER IF EXISTS chain_head_guard ON audit.chain_head;
    CREATE TRIGGER chain_head_guard
      BEFORE UPDATE OR DELETE ON audit.chain_head
      FOR EACH ROW EXECUTE FUNCTION audit.guard_chain_head();
    """)
