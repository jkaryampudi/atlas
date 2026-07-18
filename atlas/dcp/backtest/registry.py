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

from sqlalchemy import text
from sqlalchemy.orm import Session


def register_trial(session: Session, *, family: str, spec: dict[str, object],
                   metrics: dict[str, float], lineage: str,
                   hypothesis: str | None = None,
                   dataset_version: str | None = None) -> str:
    """Register one backtest trial. `lineage` is REQUIRED (ADR-0016): the DB
    column stays nullable for legacy rows, but no new trial may be registered
    without naming the research line whose penalty it inflates."""
    if not lineage or not lineage.strip():
        raise ValueError("lineage is required (ADR-0016): every new trial "
                         "names the research line it counts against")
    spec_hash = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    rid = session.execute(text(
        "INSERT INTO quant.trial_registry "
        "(strategy_family, spec_hash, metrics, lineage, hypothesis, dataset_version) "
        "VALUES (:f, :h, CAST(:m AS jsonb), :lin, :hyp, :dv) RETURNING id"),
        {"f": family, "h": spec_hash, "m": json.dumps(metrics),
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
