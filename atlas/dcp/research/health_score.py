"""Atlas HEALTH SCORE — our own composite factor-percentile ranking of a stock
against the S&P 500 universe, filling the pro-research report's PROPRIETARY
"Financial Health" panel with a MECHANICAL score we compute ourselves and label
as ours. We never copy their 1-5 pillar scores; these are Atlas's own.

FIVE PILLARS, each the mean of its factor PERCENTILES vs every active US single
name in the universe (higher percentile = healthier on that factor):

  * Relative Value   — earnings / EBITDA / book / sales YIELDS (inverted
                       multiples: cheaper ranks higher).
  * Profitability    — ROE, gross / operating / profit margins.
  * Growth           — revenue and earnings YoY growth.
  * Cash Flow        — free-cash-flow yield and FCF margin.
  * Price Momentum   — trailing 1-year PRICE return (raw closes, split-excluded;
                       dividends are not reinvested — a price return, not total).

Each factor is constructed so HIGHER = HEALTHIER, percentiled against the
universe distribution (fraction of names at or below the stock), averaged into a
pillar (0-100 + a 1-5 rating), and the pillars averaged into a composite.

HONESTY (Constitution: no invented numbers). Every factor is a reported fact or
a mechanical ratio of reported facts; a missing factor is skipped (never a
fabricated 0), a pillar with no available factor is None, and the composite
averages only the pillars that computed. Percentiles are relative ranks, not
predictions — this is a descriptive health read, MEASURED and NEVER APPLIED (it
reaches no sizing / pricing / execution).

POINT-IN-TIME. Bounded to `as_of`: every peer's fundamentals snapshot is the
latest with as_of <= as_of, the FCF period-end is <= as_of, and momentum uses
closes <= as_of. Names that had a split inside the momentum window are dropped
from that one pillar (raw closes would otherwise show a false return).

EFFICIENCY. Two queries: one jsonb extraction over the whole universe's latest
fundamentals (all fundamental factors + latest FCF), and one bounded-window
momentum query. No per-name payload fetch.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

VENDOR_SOURCE = "EodhdAdapter"
_MOM_WINDOW_DAYS = 400          # bounded scan; ~1y of sessions plus slack
_MOM_LAG = 252                  # trading sessions ~ 1 year


def _f(x: object) -> float | None:
    """Safe float of a vendor text/number value, else None (rejects NaN/inf)."""
    if x is None:
        return None
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _ratio(num: float | None, den: float | None) -> float | None:
    return (num / den) if (num is not None and den is not None and den > 0) else None


def _inv(x: float | None) -> float | None:
    """Yield = 1/multiple, only for a strictly positive multiple (a negative or
    zero P/E, EV/EBITDA, P/B, P/S is not a meaningful cheapness signal)."""
    return (1.0 / x) if (x is not None and x > 0) else None


# Each pillar lists the factor keys it averages. Factors are built so that a
# HIGHER value is always HEALTHIER (yields, margins, growth, momentum).
_PILLARS: dict[str, tuple[str, ...]] = {
    "relative_value": ("earnings_yield", "ebitda_yield", "book_yield", "sales_yield"),
    "profitability": ("roe", "gross_margin", "operating_margin", "profit_margin"),
    "growth": ("revenue_growth", "earnings_growth"),
    "cash_flow": ("fcf_yield", "fcf_margin"),
    "momentum": ("return_1y",),
}
_PILLAR_LABELS = {
    "relative_value": "Relative Value", "profitability": "Profitability",
    "growth": "Growth", "cash_flow": "Cash Flow", "momentum": "Price Momentum",
}


def _factors_from_row(r: object) -> dict[str, float | None]:
    """The fundamental factors for one universe row (raw vendor text fields),
    each constructed so higher = healthier. Momentum is attached separately."""
    mcap = _f(getattr(r, "mcap"))
    rev = _f(getattr(r, "rev"))
    gp = _f(getattr(r, "gp"))
    fcf = _f(getattr(r, "fcf"))
    return {
        "earnings_yield": _inv(_f(getattr(r, "pe"))),
        "ebitda_yield": _inv(_f(getattr(r, "evebitda"))),
        "book_yield": _inv(_f(getattr(r, "pb"))),
        "sales_yield": _inv(_f(getattr(r, "ps"))),
        "roe": _f(getattr(r, "roe")),
        "gross_margin": _ratio(gp, rev),
        "operating_margin": _f(getattr(r, "om")),
        "profit_margin": _f(getattr(r, "pm")),
        "revenue_growth": _f(getattr(r, "rg")),
        "earnings_growth": _f(getattr(r, "eg")),
        "fcf_yield": _ratio(fcf, mcap),
        "fcf_margin": _ratio(fcf, rev),
    }


def _universe_fundamentals(session: Session,
                           as_of: date) -> dict[str, dict[str, float | None]]:
    """id -> fundamental factor dict for every active US single name, from each
    name's latest fundamentals snapshot <= as_of (one jsonb query)."""
    rows = session.execute(text(
        "SELECT i.id AS id, "
        " f.p->'Highlights'->>'MarketCapitalization' AS mcap, "
        " f.p->'Valuation'->>'TrailingPE' AS pe, "
        " f.p->'Valuation'->>'EnterpriseValueEbitda' AS evebitda, "
        " f.p->'Valuation'->>'PriceBookMRQ' AS pb, "
        " f.p->'Valuation'->>'PriceSalesTTM' AS ps, "
        " f.p->'Highlights'->>'ReturnOnEquityTTM' AS roe, "
        " f.p->'Highlights'->>'GrossProfitTTM' AS gp, "
        " f.p->'Highlights'->>'RevenueTTM' AS rev, "
        " f.p->'Highlights'->>'OperatingMarginTTM' AS om, "
        " f.p->'Highlights'->>'ProfitMargin' AS pm, "
        " f.p->'Highlights'->>'QuarterlyRevenueGrowthYOY' AS rg, "
        " f.p->'Highlights'->>'QuarterlyEarningsGrowthYOY' AS eg, "
        " cf.fcf AS fcf "
        "FROM market.instruments i "
        "JOIN LATERAL (SELECT payload AS p FROM market.fundamentals f2 "
        "              WHERE f2.instrument_id = i.id AND f2.as_of <= :on "
        "              ORDER BY f2.as_of DESC LIMIT 1) f ON true "
        # jsonb_each throws on a non-object argument; a single name whose
        # Cash_Flow.yearly is a JSON null/array/scalar (EODHD does send these)
        # would otherwise abort the WHOLE universe scan. Guard on jsonb_typeof so
        # a malformed node yields no FCF row (fail-soft) instead of crashing.
        "LEFT JOIN LATERAL (SELECT je.value->>'freeCashFlow' AS fcf "
        "              FROM jsonb_each(CASE WHEN jsonb_typeof("
        "                   f.p->'Financials'->'Cash_Flow'->'yearly') = 'object' "
        "                   THEN f.p->'Financials'->'Cash_Flow'->'yearly' "
        "                   ELSE '{}'::jsonb END) je "
        "              WHERE je.key ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' "
        "                AND je.key <= :on_s ORDER BY je.key DESC LIMIT 1) cf ON true "
        "WHERE i.is_active AND i.market = 'US' "
        "  AND i.instrument_type IN ('stock','adr')"),
        {"on": as_of, "on_s": as_of.isoformat()}).all()
    return {str(r.id): _factors_from_row(r) for r in rows}


def _universe_momentum(session: Session, as_of: date) -> dict[str, float]:
    """id -> trailing 1-year return for every active US single name, from raw
    closes in a bounded window. Names with a split inside the window are dropped
    (raw closes would show a false return); most names have no split, so raw ==
    split-adjusted for them."""
    lo = as_of - timedelta(days=_MOM_WINDOW_DAYS)
    rows = session.execute(text(
        "WITH b AS ("
        "  SELECT pb.instrument_id AS iid, pb.close AS close, "
        "         row_number() OVER (PARTITION BY pb.instrument_id "
        "                            ORDER BY pb.bar_date DESC) AS rn "
        "  FROM market.price_bars_daily pb "
        "  JOIN market.instruments i ON i.id = pb.instrument_id "
        "  WHERE pb.source = :src AND pb.close IS NOT NULL "
        "    AND pb.bar_date <= :on AND pb.bar_date >= :lo "
        "    AND i.is_active AND i.market = 'US' "
        "    AND i.instrument_type IN ('stock','adr')) "
        "SELECT iid, "
        "       max(close) FILTER (WHERE rn = 1) AS last, "
        "       max(close) FILTER (WHERE rn = :lag) AS prior "
        "FROM b WHERE rn IN (1, :lag) GROUP BY iid"),
        {"src": VENDOR_SOURCE, "on": as_of, "lo": lo, "lag": _MOM_LAG}).all()
    # instruments with a split inside the window -> excluded (false raw return)
    split_ids = {str(r.iid) for r in session.execute(text(
        "SELECT DISTINCT instrument_id AS iid FROM market.corporate_actions "
        "WHERE action_type = 'split' AND action_date > :lo AND action_date <= :on"),
        {"lo": lo, "on": as_of}).all()}
    out: dict[str, float] = {}
    for r in rows:
        iid = str(r.iid)
        if iid in split_ids or r.last is None or r.prior is None or float(r.prior) <= 0:
            continue
        out[iid] = float(r.last) / float(r.prior) - 1.0
    return out


def _percentile(value: float, population: list[float]) -> float:
    """Fraction of the population at or below `value` (0..1)."""
    return sum(1 for p in population if p <= value) / len(population)


def _rating(score01: float) -> int:
    """0..1 -> a 1..5 rating (quintile), clamped."""
    return max(1, min(5, int(score01 * 5) + 1))


def compute_health_score(session: Session, instrument_id: str, symbol: str,
                         as_of: date) -> dict[str, object]:
    """Atlas's composite health score for one stock at `as_of` (see module
    docstring). Pillars/composite are None where inputs are insufficient
    (fail-soft, never fabricated)."""
    universe = _universe_fundamentals(session, as_of)
    momentum = _universe_momentum(session, as_of)
    for iid, ret in momentum.items():
        if iid in universe:
            universe[iid]["return_1y"] = ret

    subject = universe.get(str(instrument_id))
    empty: dict[str, object] = {
        "as_of": as_of.isoformat(), "universe_n": len(universe),
        "pillars": {}, "composite": {"score": None, "rating": None, "n_pillars": 0},
        "note": ("Atlas's own factor percentiles vs the S&P 500 universe — our "
                 "mechanical health read, not a vendor's proprietary score"),
    }
    if subject is None:
        return empty

    # per-factor universe distributions (non-null values only)
    all_factors = [f for keys in _PILLARS.values() for f in keys]
    dists: dict[str, list[float]] = {f: [] for f in all_factors}
    for u in universe.values():
        for f in all_factors:
            v = u.get(f)
            if v is not None:
                dists[f].append(v)

    pillars: dict[str, object] = {}
    pillar_scores: list[float] = []
    for pkey, fkeys in _PILLARS.items():
        factor_out: dict[str, object] = {}
        pctiles: list[float] = []
        for f in fkeys:
            val = subject.get(f)
            pop = dists[f]
            pct = _percentile(val, pop) if (val is not None and pop) else None
            if pct is not None:
                pctiles.append(pct)
            factor_out[f] = {"value": val, "percentile": pct}
        if pctiles:
            s01 = sum(pctiles) / len(pctiles)
            pillar_scores.append(s01)
            pillars[pkey] = {"label": _PILLAR_LABELS[pkey],
                             "score": round(s01 * 100, 1), "rating": _rating(s01),
                             "factors": factor_out}
        else:
            pillars[pkey] = {"label": _PILLAR_LABELS[pkey], "score": None,
                             "rating": None, "factors": factor_out}

    composite01 = sum(pillar_scores) / len(pillar_scores) if pillar_scores else None
    return {
        "as_of": as_of.isoformat(), "universe_n": len(universe),
        "pillars": pillars,
        "composite": {
            "score": round(composite01 * 100, 1) if composite01 is not None else None,
            "rating": _rating(composite01) if composite01 is not None else None,
            "n_pillars": len(pillar_scores),
        },
        "note": empty["note"],
    }
