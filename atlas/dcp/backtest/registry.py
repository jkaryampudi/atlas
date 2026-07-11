"""Trial registry (ADR-0002 #1): EVERY backtest is registered; deflated Sharpe
must use the true count for the strategy family."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import text
from sqlalchemy.orm import Session


def register_trial(session: Session, *, family: str, spec: dict[str, object],
                   metrics: dict[str, float]) -> str:
    spec_hash = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    rid = session.execute(text(
        "INSERT INTO quant.trial_registry (strategy_family, spec_hash, metrics) "
        "VALUES (:f, :h, CAST(:m AS jsonb)) RETURNING id"),
        {"f": family, "h": spec_hash, "m": json.dumps(metrics)}).scalar_one()
    return str(rid)


def trial_count(session: Session, family: str) -> int:
    return int(session.execute(text(
        "SELECT count(*) FROM quant.trial_registry WHERE strategy_family = :f"),
        {"f": family}).scalar() or 0)
