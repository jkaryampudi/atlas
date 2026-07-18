"""Learning surface (learning loop v1): the calibration snapshot and the
graded-label corpus, read-only. SURFACING ONLY — nothing served here is
applied anywhere (Article 10: computing/storing conviction weights is Tier-1
territory, but application awaits the Principal's activation decision), and
the payload says so explicitly so no reader can mistake a measurement for a
behavior.

Weights come from the LATEST learning.agent_calibration snapshot (the stored
fact); the derived rates (specialist alignment / flag validation, per-source
vindication vs the dartboard) are recomputed at read time from
learning.outcome_labels through the same pure functions the snapshot used —
same inputs, same numbers. Decimals cross to floats deliberately: display
analytics, never ledger money."""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from atlas.core.db import session_scope
from atlas.dcp.learning.recalibrate import (read_labels, source_trust,
                                            specialist_reliability)

router = APIRouter()

RECENT_LESSONS = 10


@router.get("/summary")
def summary() -> dict[str, object]:
    with session_scope() as s:
        memo_labels, spec_labels = read_labels(s)
        latest_period = s.execute(text(
            "SELECT max(period) FROM learning.agent_calibration "
            "WHERE regime = 'all'")).scalar()
        cal_rows = [] if latest_period is None else s.execute(text(
            "SELECT agent_role, n_forecasts, brier_score, conviction_weight, "
            "       prev_weight "
            "FROM learning.agent_calibration "
            "WHERE regime = 'all' AND period = :p "
            "ORDER BY agent_role"), {"p": latest_period}).all()
        lesson_count = s.execute(text(
            "SELECT count(*) FROM learning.lessons")).scalar() or 0
        recent = s.execute(text(
            "SELECT lesson, tags, created_at FROM learning.lessons "
            "ORDER BY created_at DESC, id LIMIT :n"),
            {"n": RECENT_LESSONS}).all()

    weights = {r.agent_role: {
        "weight": float(r.conviction_weight),
        "brier": None if r.brier_score is None else float(r.brier_score),
        "n": int(r.n_forecasts),
        "prev_weight": None if r.prev_weight is None else float(r.prev_weight)}
        for r in cal_rows}
    by_conviction = {k.removeprefix("conviction:"): v
                     for k, v in weights.items()
                     if k.startswith("conviction:")}
    by_specialist_w = {k.removeprefix("specialist:"): v
                       for k, v in weights.items()
                       if k.startswith("specialist:")}
    by_source_w = {k.removeprefix("source:"): v for k, v in weights.items()
                   if k.startswith("source:")}

    specialists = {rel.role: {
        "alignment_rate": rel.alignment_rate, "n_graded": rel.n_graded,
        "flag_validation_rate": rel.flag_validation_rate,
        "n_flagged": rel.n_flagged,
        **(by_specialist_w.get(rel.role) or {})}
        for rel in specialist_reliability(spec_labels)}
    sources = {src: {
        **{f"h{h}": {"rate": t.rate, "baseline": t.baseline, "edge": t.edge,
                     "n_graded": t.n_graded, "n_vindicated": t.n_vindicated}
           for h, t in by_h.items() if t.n_graded},
        **(by_source_w.get(src) or {})}
        for src, by_h in source_trust(memo_labels).items()}

    graded = [ml for ml in memo_labels if ml.direction_vindicated is not None]
    return {
        "labels": {
            "memo": len(memo_labels), "specialist": len(spec_labels),
            "graded_directional": len(graded),
            "by_horizon": {str(h): sum(1 for ml in memo_labels
                                       if ml.horizon_sessions == h)
                           for h in (20, 60)}},
        "calibration": {"as_of": latest_period,
                        "by_conviction": by_conviction},
        "specialists": specialists,
        "sources": sources,
        "lessons": {"count": int(lesson_count),
                    "recent": [{"lesson": r.lesson, "tags": list(r.tags or []),
                                "created_at": r.created_at.isoformat()}
                               for r in recent]},
        "applied": False,
        "note": ("surfacing only — weights are computed and stored nightly, "
                 "never applied; activation is a Principal decision "
                 "(Constitution Article 10)")}
