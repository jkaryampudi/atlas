"""learning.outcome_labels — memo-outcome label columns (learning loop v1).

WHY A MIGRATION (CLAUDE.md working style: never edit applied migrations; new
columns only when the schema genuinely lacks them): 0002's outcome_labels
columns were designed for POSITION-exit labels — exit_reason_predicted/actual,
kill_criteria_fired, the pnl_signal/timing/cost decomposition, holding_days.
None of the memo-scorecard label fields exist, and reusing those columns for
different semantics (excess in pnl_signal, direction_vindicated in
kill_criteria_fired) would corrupt the table's meaning. The position-label
columns stay: position outcome labeling remains future work on the same table.

WHAT LANDS HERE (written by atlas/dcp/learning/labeling.py once a
research.memo_outcomes row matures — 20/60 sessions, migration 0016):

- label_kind 'memo': one row per matured (memo, horizon) — the desk's call
  graded by the scorecard's one rule (direction_vindicated =
  scorecard.vindicated(); NULL for HOLD/WATCHLIST/other non-directional and
  for shadow memos — tracked, never rated), with the memo's conviction /
  source / shadow and the realized excess snapshotted at labeling time.
- label_kind 'specialist': one row per matured (memo, horizon, panel role)
  grading each research.memo_specialists assessment (0025) against the
  realized excess sign — the mapping lives in labeling.py, one place.

Append-only by convention (memo_outcomes discipline): a label, once written,
is a historical fact — never UPDATEd or DELETEd. Idempotency is the partial
unique index below (specialist_role coalesced so memo-kind rows collide with
memo-kind rows); ON CONFLICT DO NOTHING is the belt-and-braces under
concurrency. Rows from the original position-label design (all new columns
NULL) are outside the index — the table is empty today, so in practice every
row carries label_kind.

Revision ID: 0030
"""
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE learning.outcome_labels
      ADD COLUMN label_kind text CHECK (label_kind IN ('memo','specialist')),
      ADD COLUMN horizon_sessions int CHECK (horizon_sessions IN (20, 60)),
      ADD COLUMN recommendation text,
      ADD COLUMN conviction text,
      ADD COLUMN source text,
      ADD COLUMN shadow boolean,
      ADD COLUMN direction_vindicated boolean,
      ADD COLUMN excess numeric(12,6),
      ADD COLUMN specialist_role text,
      ADD COLUMN specialist_stance text,
      ADD COLUMN specialist_confidence text,
      ADD COLUMN aligned boolean,
      ADD COLUMN flag_validated boolean,
      ADD COLUMN n_red_flags int;

    -- one label per (memo, horizon, kind, seat): the labeling idempotency rule
    CREATE UNIQUE INDEX uq_outcome_labels_memo_label
      ON learning.outcome_labels
      (thesis_memo_id, horizon_sessions, label_kind,
       COALESCE(specialist_role, ''))
      WHERE label_kind IS NOT NULL;
    """)


def downgrade() -> None:
    op.execute("""
    DROP INDEX IF EXISTS learning.uq_outcome_labels_memo_label;
    ALTER TABLE learning.outcome_labels
      DROP COLUMN IF EXISTS label_kind,
      DROP COLUMN IF EXISTS horizon_sessions,
      DROP COLUMN IF EXISTS recommendation,
      DROP COLUMN IF EXISTS conviction,
      DROP COLUMN IF EXISTS source,
      DROP COLUMN IF EXISTS shadow,
      DROP COLUMN IF EXISTS direction_vindicated,
      DROP COLUMN IF EXISTS excess,
      DROP COLUMN IF EXISTS specialist_role,
      DROP COLUMN IF EXISTS specialist_stance,
      DROP COLUMN IF EXISTS specialist_confidence,
      DROP COLUMN IF EXISTS aligned,
      DROP COLUMN IF EXISTS flag_validated,
      DROP COLUMN IF EXISTS n_red_flags;
    """)
