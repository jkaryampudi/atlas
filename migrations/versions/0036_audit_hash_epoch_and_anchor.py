"""Audit-chain hash epoch + protected tail anchor (F-019, F-020).

F-019 — the chain link hash covered only prev|payload|event_type|created_at, so
an event's actor/entity identity sat OUTSIDE the chain and could be rewritten
undetected. audit.decision_events gains ``hash_version smallint NOT NULL
DEFAULT 1``: every existing row stays epoch 1 (its stored prev_hash chain still
verifies byte-identically), and every NEW append is written under epoch 2, which
folds entity_type/id + actor_type/id into the link hash (atlas/core/audit.py).

F-020 — an interior walk cannot detect deletion/replacement of the FINAL events,
because the surviving prefix stays internally valid. audit.chain_head is an
independently-recorded terminal anchor (single row): last_seq, last_chain_hash,
event_count, epoch. PostgresAuditLog.append updates it atomically with each
append; verify() compares the walked terminal state against it, so tail
truncation / terminal replacement / head rollback are caught.

The anchor is protected from ordinary rewrites by a BEFORE UPDATE/DELETE trigger
that only permits the audit writer's own controlled update (a session GUC the
writer sets inside its transaction). This closes the application-level finding;
the residual risk of a fully-privileged DBA who can alter both events and the
anchor (and disable triggers) is documented in ATLAS_OPERATOR_ACTIONS.md.

This migration does NOT rewrite or re-hash any legacy event, and never prints or
reproduces any secret in a legacy payload — it reads only seq/hashes/timestamps
to seed the anchor.

Revision ID: 0036
"""
import hashlib

from alembic import op
from sqlalchemy import text

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

GENESIS = "0" * 64


def _legacy_chain_hash(prev_hash: str, payload_hash: str, event_type: str,
                       created_at) -> str:
    # epoch-1 formula, replicated so the migration needs no app import.
    material = f"{prev_hash}|{payload_hash}|{event_type}|{created_at.isoformat()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def upgrade() -> None:
    op.execute("""
    ALTER TABLE audit.decision_events
      ADD COLUMN IF NOT EXISTS hash_version smallint NOT NULL DEFAULT 1;

    CREATE TABLE IF NOT EXISTS audit.chain_head (
        id             smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
        last_seq       bigint   NOT NULL,
        last_chain_hash text    NOT NULL,
        event_count    bigint   NOT NULL,
        epoch          smallint NOT NULL,
        updated_at     timestamptz NOT NULL DEFAULT now());

    -- Protect the anchor: it may only be written when the connection sets the
    -- guard GUC atlas.audit_writer='on' (the audit repository sets it inside its
    -- own transaction, right before the atomic append+anchor update).
    CREATE OR REPLACE FUNCTION audit.guard_chain_head() RETURNS trigger AS $$
    BEGIN
      IF current_setting('atlas.audit_writer', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'audit.chain_head is protected — only the audit writer '
          'may update it (F-020 anchor)';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    DROP TRIGGER IF EXISTS chain_head_guard ON audit.chain_head;
    CREATE TRIGGER chain_head_guard
      BEFORE UPDATE OR DELETE ON audit.chain_head
      FOR EACH ROW EXECUTE FUNCTION audit.guard_chain_head();
    """)

    # Seed the anchor from the current chain (all legacy epoch-1 rows).
    conn = op.get_bind()
    last = conn.execute(text(
        "SELECT seq, prev_hash, payload_hash, event_type, created_at "
        "FROM audit.decision_events ORDER BY seq DESC LIMIT 1")).mappings().first()
    count = conn.execute(text(
        "SELECT count(*) FROM audit.decision_events")).scalar() or 0
    if last is not None:
        chain_hash = _legacy_chain_hash(
            last["prev_hash"], last["payload_hash"], last["event_type"], last["created_at"])
        last_seq = last["seq"]
    else:
        chain_hash, last_seq = GENESIS, 0
    # the trigger allows the seed insert (INSERT is not guarded; only UPDATE/DELETE).
    conn.execute(text(
        "INSERT INTO audit.chain_head (id, last_seq, last_chain_hash, event_count, epoch) "
        "VALUES (1, :s, :h, :c, 1) ON CONFLICT (id) DO NOTHING"),
        {"s": last_seq, "h": chain_hash, "c": count})


def downgrade() -> None:
    # Clean, reversible downgrade (the repo's migration-cycle contract). NOTE: a
    # PRODUCTION downgrade strips the epoch-2 identity hash + the tail anchor, so
    # any epoch-2 events would afterwards verify only under the legacy formula —
    # an operator must re-run verify-chain and re-seed the anchor after a real
    # downgrade. The F-020 runtime protections (anchor comparison + the write
    # guard) are what enforce integrity while epoch 2 is live; the migration
    # itself stays reversible so downgrade/upgrade cycles remain clean.
    op.execute("""
    DROP TRIGGER IF EXISTS chain_head_guard ON audit.chain_head;
    DROP FUNCTION IF EXISTS audit.guard_chain_head();
    DROP TABLE IF EXISTS audit.chain_head;
    ALTER TABLE audit.decision_events DROP COLUMN IF EXISTS hash_version;
    """)
