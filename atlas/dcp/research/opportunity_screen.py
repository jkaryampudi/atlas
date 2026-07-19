"""OPPORTUNITY SCREEN — a ranked, deterministic read of the whole universe using
everything Atlas measures about a name: the health score (value / profitability /
growth / cash-flow / momentum percentiles vs the S&P 500), then, for the top
names, Atlas's own valuation verdict and the fragility markers.

MEASURED, NEVER APPLIED (same doctrine as source_picks / health_score). This is a
RESEARCH AID — a ranked candidate board for the Principal to eyeball and choose
which names to spend the desk on. It reaches NO sizing / pricing / execution, and
a systematic rule built on this ranking (buy the top-K, say) would have to clear
the full gauntlet (null model, deflated Sharpe, walk-forward) and a signature
before a cent moves — exactly like any factor. The screen surfaces ideas; it
never moves capital.

COST. Entirely deterministic — ZERO model spend. One universe query ranks all
names by health composite; only the top-N are enriched with the (per-name)
valuation and autopsy, so the cost stays bounded no matter the universe size.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.dcp.research.autopsy import compute_autopsy
from atlas.dcp.research.health_score import (
    _PILLARS,
    _PILLAR_LABELS,
    _percentile,
    _rating,
    _universe_fundamentals,
    _universe_momentum,
)
from atlas.dcp.research.source_picks import record_pick
from atlas.dcp.research.stock_models import compute_models
from atlas.dcp.research.valuation_models import compute_valuation

_TOP_N = 25

# the board's picks are tracked under this source so the existing source-pick
# grading (grade_picks / source_edge_report) measures the screen's edge vs SPY
# and a dartboard — the ONE honest question before any rule could be proposed.
SCREEN_SOURCE = "atlas-opportunity-screen"


def _score_name(factors: dict[str, float | None],
                dists: dict[str, list[float]]) -> dict[str, object]:
    """One name's health pillars + composite from its factors and the universe
    factor distributions (same rule as health_score, computed for every name)."""
    pillar_scores: list[float] = []
    pillars: dict[str, object] = {}
    for pkey, fkeys in _PILLARS.items():
        pctiles: list[float] = []
        for f in fkeys:
            val = factors.get(f)
            pop = dists.get(f) or []
            if val is not None and pop:
                pctiles.append(_percentile(val, pop))
        if pctiles:
            s01 = sum(pctiles) / len(pctiles)
            pillar_scores.append(s01)
            pillars[pkey] = {"label": _PILLAR_LABELS[pkey],
                             "score": round(s01 * 100, 1), "rating": _rating(s01)}
        else:
            pillars[pkey] = {"label": _PILLAR_LABELS[pkey], "score": None,
                             "rating": None}
    composite01 = sum(pillar_scores) / len(pillar_scores) if pillar_scores else None
    return {
        # composite is the ROUNDED display value; composite_raw (0..1, full
        # precision) is what the board sorts on — ranking on the 0.1-rounded
        # value would collapse a ~500-name universe into 1001 buckets and let a
        # healthier name lose its rank to a tie (adversarial review 2026-07).
        "composite": round(composite01 * 100, 1) if composite01 is not None else None,
        "composite_raw": composite01,
        "rating": _rating(composite01) if composite01 is not None else None,
        "n_pillars": len(pillar_scores),
        "pillars": pillars,
    }


def screen_opportunities(session: Session, as_of: date, *,
                         top_n: int = _TOP_N) -> dict[str, object]:
    """Rank the active US universe by health composite, then enrich the top-N
    with Atlas's valuation verdict + fragility markers. A ranked candidate board
    (see module docstring) — deterministic, no model spend, measured-never-applied.
    """
    universe = _universe_fundamentals(session, as_of)
    for iid, ret in _universe_momentum(session, as_of).items():
        if iid in universe:
            universe[iid]["return_1y"] = ret

    all_factors = [f for keys in _PILLARS.values() for f in keys]
    dists: dict[str, list[float]] = {f: [] for f in all_factors}
    for u in universe.values():
        for f in all_factors:
            v = u.get(f)
            if v is not None:
                dists[f].append(v)

    scored: list[tuple[str, dict[str, object]]] = []
    for iid, factors in universe.items():
        s = _score_name(factors, dists)
        if s["composite"] is not None:
            scored.append((iid, s))
    # sort on the full-precision composite (desc), with instrument id as a
    # deterministic tiebreak so the top-N is total and reproducible regardless of
    # Postgres scan order (the universe query has no ORDER BY).
    scored.sort(key=lambda t: (-float(t[1]["composite_raw"]), t[0]))  # type: ignore[arg-type]
    top = scored[:top_n]

    # symbol + sector for the shortlist, in one lookup
    ids = [iid for iid, _s in top]
    meta = {str(r.id): (r.symbol, r.sector_gics) for r in session.execute(text(
        "SELECT id, symbol, sector_gics FROM market.instruments "
        "WHERE id = ANY(:ids)"), {"ids": ids}).all()} if ids else {}

    board: list[dict[str, object]] = []
    for rank, (iid, s) in enumerate(top, start=1):
        symbol, sector = meta.get(iid, (None, None))
        if symbol is None:
            continue
        val = compute_valuation(session, iid, symbol, as_of)
        models = compute_models(session, iid, symbol, as_of)
        autopsy = compute_autopsy(models, val)
        vsum = val.get("summary")
        vsum = vsum if isinstance(vsum, dict) else {}
        tech = models.get("technical")
        tech = tech if isinstance(tech, dict) else {}
        raw_pillars = s.get("pillars")
        pillars = raw_pillars if isinstance(raw_pillars, dict) else {}
        board.append({
            "rank": rank, "symbol": symbol, "sector": sector,
            "health_composite": s.get("composite"), "health_rating": s.get("rating"),
            "pillars": {k: (p.get("score") if isinstance(p, dict) else None)
                        for k, p in pillars.items()},
            "price": val.get("price"),
            "valuation_verdict": vsum.get("verdict"),
            "valuation_basis": vsum.get("valuation_basis"),
            "upside_to_central_pct": vsum.get("upside_to_central_pct"),
            "technical_trend": tech.get("summary"),
            "fragility": autopsy.get("level"),
            "fragility_alerts": autopsy.get("n_alerts"),
        })

    return {
        "as_of": as_of.isoformat(), "universe_n": len(universe),
        "ranked_n": len(scored), "top_n": top_n, "board": board,
        "note": ("Ranked by Atlas's health composite (value / profitability / "
                 "growth / cash-flow / momentum percentiles vs the S&P 500); the "
                 "top names carry Atlas's valuation verdict and fragility markers. "
                 "A research candidate board — measured, NEVER a path to capital; "
                 "any rule built on it must clear the gauntlet first."),
    }


def snapshot_board_picks(session: Session, recommendation_date: date, *,
                         top_k: int = 20) -> list[tuple[str, str]]:
    """Record the board's top-K as MEASURED source-picks (source=SCREEN_SOURCE),
    each with the point-in-time feature snapshot record_pick captures, so the
    existing grade_picks / source_edge_report machinery answers, after a few
    months, whether the screen's leaders beat SPY (and a dartboard) at 5/10/20/60
    sessions. This is the ONLY honest way to earn the right to ever propose a rule
    on the screen — and it stays MEASURED, NEVER APPLIED: a research.source_picks
    row is tracked and scored, never bridged to a proposal or an order (invariant
    2; same plane as every other source-pick). Idempotent per (source, ticker,
    recommendation_date), so a monthly re-run is safe. Uses only stored data — no
    vendor fetch, no model spend. Returns [(symbol, outcome)] with outcome in
    {recorded, duplicate, no-data}."""
    board = screen_opportunities(session, recommendation_date, top_n=top_k)
    rows = board["board"]
    rows = rows if isinstance(rows, list) else []
    out: list[tuple[str, str]] = []
    for r in rows:
        symbol = r["symbol"] if isinstance(r, dict) else None
        if symbol is None:
            continue
        iid = session.execute(text(
            "SELECT id FROM market.instruments WHERE symbol = :s AND is_active "
            "AND market = 'US' ORDER BY id LIMIT 1"), {"s": symbol}).scalar()
        if iid is None:
            out.append((str(symbol), "no-data"))
            continue
        as_of = session.execute(text(
            "SELECT max(bar_date) FROM market.price_bars_daily "
            "WHERE instrument_id = :iid AND bar_date <= :on"),
            {"iid": str(iid), "on": recommendation_date}).scalar()
        if as_of is None:
            out.append((str(symbol), "no-data"))
            continue
        pid = record_pick(session, source=SCREEN_SOURCE, ticker=str(symbol),
                          instrument_id=str(iid), recommendation_date=recommendation_date,
                          as_of_session=as_of)
        out.append((str(symbol), "recorded" if pid is not None else "duplicate"))
    return out
