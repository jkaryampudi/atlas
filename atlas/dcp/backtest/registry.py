"""Trial registry (ADR-0002 #1): EVERY backtest is registered; deflated Sharpe
must use the true count for the strategy family.

Provenance (ADR-0011 step 1, roadmap 0.1 gap-fill): a trial MAY pin the
hypothesis it tests and the feature-store dataset_version it ran on (see
atlas/dcp/features/store.py for the hash definition). Both default to None —
every existing caller is unchanged, and historical rows honestly stay NULL.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import text
from sqlalchemy.orm import Session


def register_trial(session: Session, *, family: str, spec: dict[str, object],
                   metrics: dict[str, float], hypothesis: str | None = None,
                   dataset_version: str | None = None) -> str:
    spec_hash = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    rid = session.execute(text(
        "INSERT INTO quant.trial_registry "
        "(strategy_family, spec_hash, metrics, hypothesis, dataset_version) "
        "VALUES (:f, :h, CAST(:m AS jsonb), :hyp, :dv) RETURNING id"),
        {"f": family, "h": spec_hash, "m": json.dumps(metrics),
         "hyp": hypothesis, "dv": dataset_version}).scalar_one()
    return str(rid)


def trial_count(session: Session, family: str) -> int:
    return int(session.execute(text(
        "SELECT count(*) FROM quant.trial_registry WHERE strategy_family = :f"),
        {"f": family}).scalar() or 0)
