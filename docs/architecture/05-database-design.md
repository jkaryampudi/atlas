# 05 — Database Design

Atlas AI Capital · v1.0 · PostgreSQL 16 · Schemas: `market`, `research`, `quant`, `risk`, `trading`, `audit`

Conventions: UUID v7 PKs; `created_at timestamptz` everywhere; soft state via explicit state-machine columns with CHECK constraints; **audit schema is INSERT-only at the role level** (no UPDATE/DELETE grants to any application role). Money in `numeric(18,6)`; currencies ISO-4217; all portfolio values also stored AUD-translated with the FX rate used.

---

## 1. `market` schema (Deterministic Compute Plane inputs)

```sql
market.instruments (
  id uuid PK,
  symbol text NOT NULL,             -- 'AVGO', 'INDA', 'INFY'
  exchange text NOT NULL,           -- 'NASDAQ','NYSE','NSE','ASX'
  market text NOT NULL CHECK (market IN ('US','IN','AU')),
  instrument_type text CHECK (instrument_type IN ('stock','etf','adr')),
  name text, sector_gics text, industry text,
  economic_exposure text[],         -- ['IN'] for ADRs/India ETFs → feeds L4 look-through
  currency char(3) NOT NULL,
  is_active boolean DEFAULT true,
  listed_at date,
  UNIQUE (symbol, exchange)
)

market.price_bars_daily (
  instrument_id uuid FK, bar_date date,
  open, high, low, close, adj_close numeric(18,6),
  volume bigint,
  source text NOT NULL, ingested_at timestamptz,
  quality_flags text[],             -- ['gap','split_suspect'] …
  PRIMARY KEY (instrument_id, bar_date)
)                                     -- partitioned by year

market.corporate_actions (
  id uuid PK, instrument_id uuid FK, action_date date,
  action_type text CHECK (action_type IN ('split','dividend','bonus','rights','symbol_change')),
  ratio numeric, amount numeric, currency char(3), source text
)

market.fundamentals_pit (             -- POINT-IN-TIME: the backtest-honesty table
  id uuid PK, instrument_id uuid FK,
  period_end date NOT NULL,          -- fiscal period the numbers describe
  published_at date NOT NULL,        -- when the market could actually know them  ← critical
  metric text NOT NULL,              -- 'revenue','eps_diluted','fcf','net_debt'…
  value numeric, currency char(3),
  restated boolean DEFAULT false, supersedes_id uuid NULL,
  source text, bias_class text CHECK (bias_class IN ('pit','asof_latest','unknown')),
  UNIQUE (instrument_id, period_end, metric, published_at)
)

market.fx_rates_daily (base char(3), quote char(3), rate_date date, rate numeric(18,8),
                       source text, PRIMARY KEY (base, quote, rate_date))

market.news_items (id uuid PK, instrument_id uuid NULL, market text,
                   headline text, body_ref text,       -- object-store pointer, not inline
                   published_at timestamptz, source text, url text,
                   trust_tier smallint)                -- injection-defence tiering

market.macro_series (series_code text, obs_date date, value numeric, source text,
                     published_at date,                -- PIT for macro too (revisions!)
                     PRIMARY KEY (series_code, obs_date, published_at))

market.data_quality_gates (market text, gate_date date,
                           status text CHECK (status IN ('green','amber','red')),
                           reasons jsonb, PRIMARY KEY (market, gate_date))
```

**Design note (Challenge A5):** `bias_class` on fundamentals and `published_at` on both fundamentals and macro are what make honest backtesting possible. The backtester refuses to join any table without a PIT timestamp discipline unless the run is explicitly flagged `bias_acknowledged` — and such runs can never support strategy approval.

## 2. `research` schema (Reasoning Plane outputs)

```sql
research.agent_runs (
  id uuid PK, agent_role text NOT NULL,
  prompt_template_hash text NOT NULL, model text NOT NULL, model_version text NOT NULL,
  input_refs jsonb NOT NULL,         -- [{type:'snapshot',id:…},{type:'memo',id:…}]
  output_ref text,                   -- object-store pointer to full structured output
  output_hash text, status text CHECK (status IN ('ok','schema_fail','timeout','budget_kill')),
  tokens_in int, tokens_out int, cost_usd numeric(10,4), latency_ms int,
  workflow_run_id uuid, created_at timestamptz
)

research.memos (
  id uuid PK, agent_run_id uuid FK, memo_type text
    CHECK (memo_type IN ('research','macro','sector','committee','pm_review',
                         'attribution','risk_narrative','validation','scanner')),
  instrument_id uuid NULL, sleeve text NULL,
  recommendation text CHECK (recommendation IN
    ('BUY','HOLD','REJECT','EXIT','REDUCE','ADD','INSUFFICIENT_EVIDENCE','N/A')),
  conviction text CHECK (conviction IN ('LOW','MEDIUM','HIGH','N/A')),
  thesis text, kill_criteria jsonb,  -- [{condition, metric_ref, threshold}]
  evidence_refs jsonb NOT NULL,      -- every claim's citations — schema-enforced
  dissent text,                      -- 'strongest case against' (Constitution 4.3)
  created_at timestamptz
)

research.watchlist (instrument_id uuid, added_by_memo uuid, status text, added_at, removed_at)
```

## 3. `quant` schema

```sql
quant.strategies (
  id uuid PK, family text,           -- 'momentum','quality_growth','regime'
  name text, version text, spec jsonb,      -- full parameterisation
  code_ref text, code_sha text,             -- git pin of the DCP implementation
  state text CHECK (state IN ('draft','backtested','validated','approved','live','paper','retired')),
  approved_by text NULL, approved_at timestamptz NULL,
  tolerance_bands jsonb,             -- pre-registered live-vs-backtest tolerances (Doc 04 §9)
  UNIQUE (family, name, version)
)

quant.backtests (
  id uuid PK, strategy_id uuid FK,
  universe_snapshot_ref text, date_start date, date_end date,
  oos_start date,                    -- holdout boundary — never moved after first run
  cost_model jsonb,                  -- commissions, spread, slippage assumptions
  data_bias_summary jsonb,           -- bias_class per dataset used
  metrics jsonb,                     -- CAGR, Sharpe, deflated Sharpe, maxDD, hit rate…
  equity_curve_ref text, trades_ref text,
  code_sha text, created_at timestamptz
)

quant.validation_reports (
  id uuid PK, strategy_id uuid FK, backtest_id uuid FK, agent_run_id uuid FK,
  checklist jsonb NOT NULL,          -- each item: {check, artifact_ref, result}
  verdict text CHECK (verdict IN ('approve','reject','revise')),
  reasons text, created_at timestamptz
)

quant.signals (
  id uuid PK, strategy_id uuid FK, instrument_id uuid FK,
  signal_date date, direction text CHECK (direction IN ('long','flat')),
  score numeric,                     -- deterministic strategy score
  features jsonb,                    -- inputs snapshot for explainability
  entry_ref numeric, stop_ref numeric, target_ref numeric,   -- DCP-computed levels
  code_sha text, created_at timestamptz,
  UNIQUE (strategy_id, instrument_id, signal_date)
)

quant.regime_states (market text, state_date date,
                     regime text CHECK (regime IN ('bull','bear','high_vol','neutral')),
                     features jsonb, strategy_id uuid FK, PRIMARY KEY (market, state_date))
```

## 4. `risk` schema

```sql
risk.limit_sets (
  id uuid PK, version int UNIQUE, mode text CHECK (mode IN ('institutional','small_aum')),
  limits jsonb NOT NULL,             -- L1..L11 with values
  effective_from date, created_by text,      -- human identity
  confirmation_a timestamptz, confirmation_b timestamptz,  -- dual confirmation (≥1h apart)
  supersedes int NULL
)                                      -- rows are never updated; new version = new row

risk.risk_checks (
  id uuid PK, proposal_id uuid FK, limit_set_version int FK,
  portfolio_snapshot_id uuid FK, price_snapshot jsonb,
  results jsonb NOT NULL,            -- [{rule:'L1', value, limit, pass}]
  verdict text CHECK (verdict IN ('PASS','FAIL')),
  check_kind text CHECK (check_kind IN ('proposal','approval_time','order_time')),
  created_at timestamptz
)

risk.stress_runs (id uuid PK, scenario_code text, portfolio_snapshot_id uuid FK,
                  proposal_id uuid NULL,        -- marginal-impact runs
                  results jsonb, nav_impact_pct numeric, dd_level_triggered text NULL,
                  created_at timestamptz)

risk.drawdown_state (as_of date PRIMARY KEY, nav_aud numeric, hwm_aud numeric,
                     drawdown_pct numeric, breaker_level text
                       CHECK (breaker_level IN ('none','DD1','DD2','DD3')),
                     cleared_by text NULL, cleared_at timestamptz NULL)

risk.halts (id uuid PK, scope text,   -- 'global','US','IN','reasoning_plane'
            reason text, triggered_by text, started_at, ended_at,
            end_confirmation_a timestamptz, end_confirmation_b timestamptz)
```

## 5. `trading` schema

```sql
trading.trade_proposals (
  id uuid PK, instrument_id uuid FK, market text,
  action text CHECK (action IN ('buy','sell','reduce','exit')),
  committee_memo_id uuid FK NOT NULL,          -- Principle 1: no trade without evidence
  signal_ids uuid[] NOT NULL,
  entry_price, stop_loss, target_price numeric NOT NULL,   -- DCP outputs
  position_size int, position_value_aud numeric,
  risk_check_id uuid FK NULL,       -- set on PASS
  thesis_summary text, risks jsonb, confidence text,
  quant_score numeric, risk_score numeric,     -- both DCP-computed
  state text CHECK (state IN ('draft','risk_review','pending_approval',
                              'approved','rejected','expired','executed','voided')),
  expires_at timestamptz NOT NULL,             -- 24h TTL
  created_at timestamptz
)

trading.approvals (id uuid PK, proposal_id uuid FK,
                   decision text CHECK (decision IN ('approve','reject')),
                   approver text NOT NULL, auth_method text, ip inet,
                   approval_time_risk_check_id uuid FK NOT NULL,   -- re-check at click
                   decided_at timestamptz)

trading.orders (
  id uuid PK, proposal_id uuid FK, approval_id uuid FK NOT NULL,
  risk_check_id uuid FK NOT NULL,
  broker text, broker_order_id text,
  side text, qty int, order_type text, limit_price numeric NULL,
  tolerance_band jsonb,             -- Trader-agent tactics clamped to DCP band
  state text CHECK (state IN ('pending_submit','submitted','partially_filled',
                              'filled','cancelled','rejected','error')),
  submitted_at, closed_at timestamptz
)

trading.executions (id uuid PK, order_id uuid FK, fill_qty int, fill_price numeric,
                    fees numeric, fx_rate_used numeric, broker_exec_id text, executed_at timestamptz)

trading.positions (id uuid PK, instrument_id uuid FK, qty int, avg_cost numeric,
                   currency char(3), opened_at, closed_at NULL,
                   current_stop numeric, thesis_memo_id uuid FK, kill_criteria jsonb)

trading.tax_lots (id uuid PK, position_id uuid FK, execution_id uuid FK,
                  qty int, cost_aud numeric, acquired_at timestamptz, disposed_at NULL,
                  proceeds_aud numeric NULL)

trading.portfolio_snapshots (id uuid PK, as_of timestamptz, nav_aud numeric,
                             cash_aud numeric, holdings jsonb, exposures jsonb,
                             fx_rates jsonb, open_risk_pct numeric)

trading.reconciliations (id uuid PK, as_of date, broker text,
                         status text CHECK (status IN ('clean','break')),
                         diffs jsonb, resolved_at NULL)
```

## 6. `audit` schema (append-only)

```sql
audit.decision_events (
  id uuid PK, seq bigserial UNIQUE,    -- gap-detectable ordering
  event_type text NOT NULL,            -- 'signal.generated','proposal.created',
                                       -- 'risk.check.completed','approval.recorded',
                                       -- 'order.state_changed','limit_set.changed',
                                       -- 'halt.triggered','agent.run.completed', …
  entity_type text, entity_id uuid,
  actor_type text CHECK (actor_type IN ('dcp','agent','human','scheduler','broker')),
  actor_id text, payload jsonb NOT NULL,
  payload_hash text NOT NULL,
  prev_hash text NOT NULL,             -- hash chain: tamper-evidence
  created_at timestamptz DEFAULT now()
)
-- Role grants: INSERT only. Verification job re-walks the hash chain nightly.

audit.compliance_reports (id uuid PK, period text, report_ref text,
                          exceptions jsonb, agent_run_id uuid FK, created_at)
```

## 7. Integrity rules that encode the principles

| Principle | Enforcement |
|---|---|
| No trade without evidence | `trade_proposals.committee_memo_id NOT NULL`, `signal_ids NOT NULL` |
| No strategy without backtest | signals FK to strategies; service reads only `state='live'`; `live` reachable only via `validated → approved` transitions (trigger-enforced) |
| No execution without risk approval | `orders.approval_id NOT NULL`, `orders.risk_check_id NOT NULL`, approval requires fresh `approval_time_risk_check_id` |
| No agent overrides risk | no agent DB role has any grant on `risk.limit_sets`; dual-confirmation columns must both be present and ≥1h apart (CHECK) |
| Auditable | INSERT-only audit role, hash chain, nightly verification |

## 8. Sizing estimate

At this universe (~700 instruments, daily bars, 10-year history): `price_bars_daily` ≈ 1.8M rows — trivial. Fundamentals PIT ≈ 1–2M rows. News bodies and agent outputs go to object storage (filesystem/S3-compatible) with DB pointers to keep Postgres lean. Redis holds only ephemeral transport and caches; Postgres is always the source of truth.

---

## Amendments v1.2 — `learning` schema + trial registry (ADR-0002/0003)

```sql
quant.trial_registry (            -- EVERY backtest ever run; deflated Sharpe uses true counts
  id uuid PK, strategy_family text, spec_hash text, backtest_id uuid FK NULL,
  metrics jsonb, created_at timestamptz)

learning.outcome_labels (
  id uuid PK, position_id uuid FK, thesis_memo_id uuid FK,
  kill_criteria_fired boolean, exit_reason_predicted text, exit_reason_actual text,
  pnl_signal numeric, pnl_timing numeric, pnl_cost numeric,
  holding_days int, labeled_at timestamptz)

learning.counterfactuals (
  id uuid PK, source_type text CHECK (source_type IN
    ('rejected_proposal','expired_proposal','stopped_position')),
  source_id uuid, tracked_from date, horizon_days int,
  hypothetical_return numeric NULL, benchmark_return numeric NULL,
  status text CHECK (status IN ('tracking','closed')))

learning.agent_calibration (
  agent_role text, period text, regime text,
  n_forecasts int, brier_score numeric,
  conviction_weight numeric, prev_weight numeric, updated_at timestamptz,
  PRIMARY KEY (agent_role, period, regime))

learning.lessons (id uuid PK, source_type text, source_id uuid,
  lesson text, tags text[], embedding_ref text, created_at timestamptz)

learning.adjustments (            -- the Tier 1 self-adjustment audit
  id uuid PK, tier smallint CHECK (tier IN (1,2)),
  target text, before jsonb, after jsonb, evidence_refs jsonb,
  reversible_to uuid NULL, created_at timestamptz)
```
Halt scopes now include `'learning'` (freeze). Agent roles receive SELECT on `learning.*` only; writes are DCP/scheduler.
