"""trade_proposals.origin — carve out the passive index core from the
agent-evidence invariant, authorised by ADR-0012 (signed 2026-07-15).

WHY THIS MIGRATION EXISTS
Migration 0010 encoded CLAUDE.md invariant 2 ("no BUY without DCP evidence")
STRUCTURALLY on trading.trade_proposals: every proposal HARD-REQUIRES a
committee memo (committee_memo_id NOT NULL), at least one signal
(signal_ids NOT NULL CHECK cardinality > 0) and a stop (stop_loss NOT NULL).

ADR-0012 deploys a passive index core (SPY 55% + INDA 15% of book) as a
DETERMINISTIC, Principal-parameterised target-weight policy: "hold the market"
is not an agent recommendation — no committee, no signal, no thesis, and the
core is rebalanced, NOT stopped. Such a proposal has NONE of the three by
design, and the directive itself (the Principal's signature on ADR-0012) is the
evidence. It enters the queue with origin 'core_allocation' and still passes the
full risk engine (invariant 3 preserved).

THE INVARIANT IS NOT WEAKENED FOR AGENTS — IT IS SCOPED.
The three requirements become ORIGIN-CONDITIONAL. A default origin of 'agent'
means every existing row and every build_proposal() insert (which does not
mention origin) is still an agent proposal bound by the FULL invariant:
  * origin='agent' with a NULL committee_memo_id  -> rejected;
  * origin='agent' with empty/absent signal_ids   -> rejected;
  * origin='agent' with a NULL stop_loss           -> rejected.
ONLY origin='core_allocation' is carved out. The carve-out is authorised by
ADR-0012 and gated strictly to that single origin value (the origin CHECK
admits no others). This is the DB half of the ADR; the deterministic
rebalancer lives in atlas/dcp/trading/core_allocation.py.

NOTE: entry_price and target_price stay NOT NULL for ALL origins — this
migration relaxes ONLY the three evidence columns named in ADR-0012's decision.
A passive-core leg records the reference close in both (a passive hold has no
price target; the reference close is the deterministic mark it was sized on).

DOWNGRADE restores the pre-0012 schema EXACTLY: the two NOT NULLs and the
blanket cardinality check come back and the origin column is dropped. Downgrade
therefore requires that no core_allocation rows exist (their NULL memo/stop
would violate the restored NOT NULLs) — the ordinary precondition for
re-tightening a constraint.

Revision ID: 0022
"""
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    -- 1. the origin discriminator; existing rows + build_proposal() default to
    --    'agent', so the full invariant keeps binding them unchanged.
    ALTER TABLE trading.trade_proposals
      ADD COLUMN origin text NOT NULL DEFAULT 'agent';
    ALTER TABLE trading.trade_proposals
      ADD CONSTRAINT trade_proposals_origin_check
      CHECK (origin IN ('agent', 'core_allocation'));

    -- 2. committee memo: required for agents, NULL allowed only for the core.
    ALTER TABLE trading.trade_proposals
      ALTER COLUMN committee_memo_id DROP NOT NULL;
    ALTER TABLE trading.trade_proposals
      ADD CONSTRAINT trade_proposals_agent_requires_memo
      CHECK ((origin <> 'agent') OR (committee_memo_id IS NOT NULL));

    -- 3. signals (invariant 2, "no BUY without DCP evidence"): drop the BLANKET
    --    cardinality check, re-add it scoped to agents. signal_ids stays
    --    NOT NULL; the core carries an EMPTY array, agents a non-empty one.
    ALTER TABLE trading.trade_proposals
      DROP CONSTRAINT trade_proposals_signal_ids_check;
    ALTER TABLE trading.trade_proposals
      ADD CONSTRAINT trade_proposals_agent_requires_signal
      CHECK ((origin <> 'agent') OR (cardinality(signal_ids) > 0));

    -- 4. stop: required for agents (ADR-0006 stops), NULL for the core, which
    --    is rebalanced, not stopped (ADR-0012).
    ALTER TABLE trading.trade_proposals
      ALTER COLUMN stop_loss DROP NOT NULL;
    ALTER TABLE trading.trade_proposals
      ADD CONSTRAINT trade_proposals_agent_requires_stop
      CHECK ((origin <> 'agent') OR (stop_loss IS NOT NULL));
    """)


def downgrade() -> None:
    op.execute("""
    -- restore the pre-0012 schema exactly (fails if core_allocation rows exist,
    -- whose NULL memo/stop cannot satisfy the restored NOT NULLs).
    ALTER TABLE trading.trade_proposals
      DROP CONSTRAINT trade_proposals_agent_requires_stop;
    ALTER TABLE trading.trade_proposals
      ALTER COLUMN stop_loss SET NOT NULL;

    ALTER TABLE trading.trade_proposals
      DROP CONSTRAINT trade_proposals_agent_requires_signal;
    ALTER TABLE trading.trade_proposals
      ADD CONSTRAINT trade_proposals_signal_ids_check
      CHECK (cardinality(signal_ids) > 0);

    ALTER TABLE trading.trade_proposals
      DROP CONSTRAINT trade_proposals_agent_requires_memo;
    ALTER TABLE trading.trade_proposals
      ALTER COLUMN committee_memo_id SET NOT NULL;

    ALTER TABLE trading.trade_proposals
      DROP CONSTRAINT trade_proposals_origin_check;
    ALTER TABLE trading.trade_proposals
      DROP COLUMN origin;
    """)
