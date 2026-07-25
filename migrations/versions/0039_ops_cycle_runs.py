"""Durable daily-cycle ledger + supervision keystone (F-025, M56, M57).

The T0-T9 daily cycle runs inside ONE atomic session_scope() transaction
(daily.py): on any failure it rolls back the workflow_runs row, all node
results, the reconciliation break row, every audit event, AND the LLM-spend
rows — so a FAILED or never-started cycle leaves no durable trace, missed cycles
are invisible (daily-2026-07-16 was silently dropped), and the cost breaker
undercounts spend across retries.

``ops.cycle_runs`` is the keystone: a per-execution ledger written on a
connection SEPARATE from the cycle's atomic transaction (atlas/ops/cycle_ledger.py),
so a 'running' claim and a terminal 'failed'/'completed' row + captured LLM spend
survive the very rollback they record. A supervisor (atlas/ops/supervise.py)
reads it to decide 'a cycle was missed / is stuck', and a partial-unique index on
(business_date) WHERE status='running' is the cross-process overlap guard the
process-local threading.Lock could never be.

status: running -> {completed | failed | refused | killed}. business_date is the
UTC date the run_id (daily-<date>) and cycle_refusal already key on. No advisory
lock (would risk an ABBA with the audit lock) — the unique index is the guard.

Revision ID: 0039
"""
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE SCHEMA IF NOT EXISTS ops;

    CREATE TABLE IF NOT EXISTS ops.cycle_runs (
        id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        business_date  date NOT NULL,       -- clock.now() UTC date; = daily-<date>
        run_id         text NOT NULL,
        trigger        text NOT NULL,       -- scheduler|manual|cli|launchd|systemd|backup
        status         text NOT NULL
                       CHECK (status IN ('running','completed','failed','refused','killed')),
        started_at     timestamptz NOT NULL,
        heartbeat_at   timestamptz,
        finished_at    timestamptz,
        exit_code      integer,
        llm_spend_usd  numeric(12,4),       -- captured before rollback (M57)
        detail         text,
        host           text,
        pid            integer,
        created_at     timestamptz NOT NULL DEFAULT now()
    );

    CREATE INDEX IF NOT EXISTS ix_cycle_runs_business_date
        ON ops.cycle_runs (business_date);

    -- Cross-process overlap guard: at most one RUNNING cycle per business date.
    -- A crashed cycle leaves a stale 'running' that blocks a retry until
    -- restart-recovery marks it 'killed' (deliberate: never double-run while one
    -- might still be alive). Terminal rows are unconstrained, so failed->retry
    -- and idempotent re-runs both append fresh history.
    CREATE UNIQUE INDEX IF NOT EXISTS uq_cycle_runs_running
        ON ops.cycle_runs (business_date) WHERE status = 'running';
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS ops.cycle_runs;
    DROP SCHEMA IF EXISTS ops;
    """)
