"""Trial registry (ADR-0002 #1): EVERY backtest is registered; deflated Sharpe
must use the true count for the strategy LINEAGE (ADR-0016, board item 9).

Lineage-scoped counting: the family name identifies one recipe (e.g.
'xsmom-impl500-tr'); the lineage names the research line it belongs to
(e.g. 'momentum'). Deflating at the family count let every freshly-named
variant evaluate at n_trials=1 — renaming a variant reset its penalty. Every
NEW registration therefore REQUIRES a lineage tag, and gates deflate at
lineage_count(). Legacy rows were backfilled by migration 0032; unknown
legacy rows honestly stay NULL. Forward-only: past verdicts are history.

Provenance (ADR-0011 step 1, roadmap 0.1 gap-fill): a trial MAY pin the
hypothesis it tests and the feature-store dataset_version it ran on (see
atlas/dcp/features/store.py for the hash definition). Both default to None —
historical rows honestly stay NULL.
"""
from __future__ import annotations

import hashlib
import json
import statistics

from sqlalchemy import text
from sqlalchemy.orm import Session

# The three metrics EVERY genuine backtest writes, whatever its family schema —
# portfolio runners add avg_turnover/n_rebalances, single-name runners add
# hit_rate/n_trades, but all real runs carry these three. A row carrying all
# three is a real out-of-sample observation; a row missing any is a synthetic/
# placeholder fixture (e.g. an exact sharpe=1.0/2.0/3.0 with no return or
# drawdown) and is NOT sampled for the cross-trial Sharpe dispersion (F-005).
# Content rule fixed before any number is read, not a threshold tuned to a DSR;
# it must stay a SUBSET common to every real schema so no genuine run is dropped.
_REAL_METRIC_KEYS: tuple[str, ...] = ("sharpe", "total_return", "max_drawdown")


# F-023: the closed catalog of reviewed research lineages. Every new trial
# deflates one of these buckets; a fresh arbitrary tag would reset the
# multiple-testing penalty. Adding a line is a reviewed change (like the
# factory feature catalog). Matches the lines present in quant.trial_registry.
KNOWN_LINEAGES: frozenset[str] = frozenset({
    "momentum", "momentum+pead", "pead", "trend", "meanrev", "breakout",
    "quality", "low-vol", "fxlab",
})


def register_trial(session: Session, *, family: str, spec: dict[str, object],
                   metrics: dict[str, float], lineage: str,
                   hypothesis: str | None = None,
                   dataset_version: str | None = None,
                   spec_hash: str | None = None) -> str:
    """Register one backtest trial. `lineage` is REQUIRED (ADR-0016): the DB
    column stays nullable for legacy rows, but no new trial may be registered
    without naming the research line whose penalty it inflates.

    `spec_hash` (traceability): a caller with a CANONICAL spec identity (e.g.
    RecipeSpec.spec_hash(), which its reports/audit events/console print) may
    pass it so the registry row is queryable by the exact hash every other
    surface quotes. When omitted, the registry digests the spec dict itself
    (the legacy behavior, unchanged for existing runners)."""
    if not lineage or not lineage.strip():
        raise ValueError("lineage is required (ADR-0016): every new trial "
                         "names the research line it counts against")
    if lineage not in KNOWN_LINEAGES:
        # F-023: the lineage is the deflation bucket. Accepting an arbitrary new
        # tag lets a variant reset its multiple-testing penalty (the exact
        # ADR-0016 defect, re-opened outside the factory). Bind to a reviewed
        # catalog: adding a research line is a reviewed change here, mirroring
        # factory.features.FEATURE_LINEAGE.
        raise ValueError(
            f"unknown lineage {lineage!r} (F-023): register only against a "
            f"reviewed research line. Known: {sorted(KNOWN_LINEAGES)}. Adding a "
            "line is a reviewed change to registry.KNOWN_LINEAGES.")
    h = (spec_hash if spec_hash is not None else hashlib.sha256(
        json.dumps(spec, sort_keys=True).encode()).hexdigest())
    rid = session.execute(text(
        "INSERT INTO quant.trial_registry "
        "(strategy_family, spec_hash, metrics, lineage, hypothesis, dataset_version) "
        "VALUES (:f, :h, CAST(:m AS jsonb), :lin, :hyp, :dv) RETURNING id"),
        {"f": family, "h": h, "m": json.dumps(metrics),
         "lin": lineage, "hyp": hypothesis, "dv": dataset_version}).scalar_one()
    return str(rid)


def trial_count(session: Session, family: str) -> int:
    """Trials registered under one exact family name. NOT the deflation count
    — gates deflate at lineage_count() (ADR-0016); this remains for exhibits
    and for the approval check that the family itself registered its run."""
    return int(session.execute(text(
        "SELECT count(*) FROM quant.trial_registry WHERE strategy_family = :f"),
        {"f": family}).scalar() or 0)


def lineage_count(session: Session, lineage: str) -> int:
    """The deflation count (ADR-0016): every registered trial in the lineage,
    across all family names it has ever worn. Rows with NULL lineage
    (unknown legacy) are not counted anywhere — a known, documented gap, not
    a silent one (see migration 0032)."""
    return int(session.execute(text(
        "SELECT count(*) FROM quant.trial_registry WHERE lineage = :l"),
        {"l": lineage}).scalar() or 0)


def lineage_sr_dispersion(session: Session, lineage: str) -> float | None:
    """Empirical cross-trial Sharpe dispersion for the Deflated-Sharpe
    expected-maximum term (F-005, BLdP). Returns the sample standard deviation of
    the annualised Sharpe ESTIMATES across the lineage's REAL registered
    backtests, or ``None`` when fewer than two exist (nothing to estimate — the
    gate then floors at ``sqrt(1/n_days)``).

    'Real' means the trial's metrics carry the full backtest signature
    (``_REAL_METRIC_KEYS``). Placeholder/synthetic rows lacking it (e.g. a fixture
    with ``sharpe`` set to an exact 1.0/2.0/3.0 and no return/drawdown) are NOT
    observations of the family's out-of-sample Sharpe distribution and would
    corrupt the estimate, so they are excluded. This is a content rule fixed
    before any number is read, not a threshold tuned to a desired DSR — the same
    filter that ``register_trial`` populates for genuine runs.

    Unlike ``lineage_count`` (which stays a conservative over-count of *bets
    placed*, synthetic rows included, because over-counting only DEFLATES), the
    dispersion is a distributional estimate and MUST be taken over genuine
    observations only. The two intentionally scope differently; see F-005."""
    key_pred = " AND ".join(f"metrics ? '{k}'" for k in _REAL_METRIC_KEYS)
    rows = session.execute(text(
        "SELECT (metrics->>'sharpe')::float FROM quant.trial_registry "
        f"WHERE lineage = :l AND {key_pred}"),
        {"l": lineage}).scalars().all()
    xs = [float(x) for x in rows if x is not None]
    if len(xs) < 2:
        return None
    return statistics.stdev(xs)
