"""Point-in-time Feature Store + trial-registry provenance (ADR-0011 step 1).

WHY THIS MIGRATION EXISTS
Every future factor (value, quality, growth) and any eventual ML needs a
versioned, reproducible, no-look-ahead store of computed factor values. Today
each strategy computes its features ad hoc (momentum inside signals/xsmom,
SUE inside signals/pead); there is no shared substrate, no dataset versioning,
and quant.trial_registry lacks hypothesis / dataset-version provenance
(roadmap 0.1/0.2 gap, sequenced by ADR-0011).

quant.feature_definitions — one row per registered feature. `name` is UNIQUE:
a feature's identity is its name, and its (version, code_sha, spec) are PINS
on that identity — the prompts-are-code discipline applied to features.
code_sha is the sha256 over the source files that constitute the computation
(atlas/dcp/features/store.py documents the exact recipe); register_feature
refuses a mismatched re-registration, so silently changed feature math cannot
write values under an old definition. created_at comes from the injected
clock (CLAUDE.md invariant 6), hence NOT NULL without a DB default.

quant.feature_values — the point-in-time values. `session_date` is the
session whose CLOSE the value is knowable AT (the no-look-ahead anchor);
`dataset_version` is a deterministic hash of the input-data extent (see
atlas/dcp/features/store.py for the exact definition), so two
materializations over identical inputs land on the same version and new data
produces a NEW version. APPEND-ONLY BY CONVENTION: a value for a session is a
fact once computed — recomputation under new data gets a new dataset_version,
never an UPDATE; the composite primary key (= the natural key the scope
requires UNIQUE) lets ON CONFLICT DO NOTHING make re-materialization a no-op.

quant.trial_registry gains `hypothesis` and `dataset_version` (both NULL) —
the roadmap-0.1 gap-fill: a future backtest CAN pin the hypothesis it tests
and the exact dataset vintage it ran on. Existing rows stay NULL — honest
history, never backfilled with guesses.

DOWNGRADE drops the two tables and the two columns; nothing else references
them (the store module is the only writer/reader in step 1).

Revision ID: 0024
"""
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE quant.feature_definitions (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      name text NOT NULL UNIQUE,
      version text NOT NULL,
      spec jsonb NOT NULL DEFAULT '{}',
      code_sha text NOT NULL,
      created_at timestamptz NOT NULL
    );

    CREATE TABLE quant.feature_values (
      feature_id uuid NOT NULL REFERENCES quant.feature_definitions(id),
      instrument_id uuid NOT NULL REFERENCES market.instruments(id),
      session_date date NOT NULL,
      value numeric NOT NULL,
      dataset_version text NOT NULL,
      computed_at timestamptz NOT NULL,
      -- the natural key: one fact per (feature, instrument, session, vintage).
      -- Append-only by convention; ON CONFLICT DO NOTHING targets this key.
      PRIMARY KEY (feature_id, instrument_id, session_date, dataset_version)
    );

    -- read path: feature_at / feature_panel scan (feature, vintage, symbol)
    -- ranges ordered by session_date.
    CREATE INDEX feature_values_read_idx
      ON quant.feature_values (feature_id, dataset_version, instrument_id,
                               session_date);

    -- Provenance columns (additive, nullable): existing rows stay NULL.
    ALTER TABLE quant.trial_registry ADD COLUMN hypothesis text;
    ALTER TABLE quant.trial_registry ADD COLUMN dataset_version text;

    -- agents may READ features as evidence (same read-only posture as market)
    GRANT SELECT ON quant.feature_definitions, quant.feature_values
      TO atlas_agent_reader;
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS quant.feature_values;
    DROP TABLE IF EXISTS quant.feature_definitions;
    ALTER TABLE quant.trial_registry DROP COLUMN IF EXISTS hypothesis;
    ALTER TABLE quant.trial_registry DROP COLUMN IF EXISTS dataset_version;
    """)
