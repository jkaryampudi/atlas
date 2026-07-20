"""low_vol_252 — realized volatility as a stored point-in-time feature:
NEGATIVE annualized population stdev of daily simple returns over the 252
sessions ending at t, on split-adjusted closes, knowable at session t's close.

WHY NEGATIVE. The recipe grammar ranks descending only (higher = better), so
the defensive end of the anomaly must be the high end of the feature: the
value is -vol, and rank-desc puts the LOWEST-volatility names first. The sign
is part of the pinned formula, not a runner option.

THE CONVENTIONS ARE MOMENTUM'S, OPERATION FOR OPERATION where they overlap
(features/momentum.py): Decimal vendor closes -> adjust_for_splits capped at
t (every close in the window — a mid-window split left raw would fabricate a
±50% return) -> float() each close -> simple returns c[i]/c[i-1] - 1 ->
population stdev (statistics.pstdev, the estimator Atlas's risk panels
already use) * sqrt(252) -> negated. The anchor test pins this compute at
window=20 byte-identical to the production risk panel's vol_20d_ann
(research/stock_models.py) on a split-bearing series.

ELIGIBILITY (fail-closed): a value exists at session t iff the instrument has
a vendor close on EVERY of the window+1 US sessions ending at t (contiguity)
and every adjusted close in the window is positive — a nonpositive close
makes a return meaningless, so the session is absent, never guessed.

NO LOOK-AHEAD, structurally: the window ends at t and only splits with
action_date <= t are applied — same query cap as momentum.

DATASET_VERSION EXTENT: identical inputs to momentum (vendor closes + split
actions <= END), declared locally so this feature's pin never couples to
momentum's source file.
"""
from __future__ import annotations

import math
import statistics
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.dcp.features.store import ComputeFn
from atlas.dcp.market_data.adjustment import adjust_for_splits
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.market_data.models import Bar, Split

VENDOR_SOURCE = "EodhdAdapter"      # same vendor-bar pin as momentum
VOL_WINDOW = 252                    # sessions of returns; needs WINDOW+1 closes
ANNUALIZATION = 252                 # sqrt(252) — the risk panels' convention
_CAL_SLACK_DAYS = 550               # calendar days that always cover 253 sessions


def make_vol_compute(window: int) -> ComputeFn:
    """The compute at an arbitrary return-window (the catalog member uses
    VOL_WINDOW=252; the equivalence anchor exercises 20 against the production
    risk panel). Kept parameterized in ONE place so the anchor test and the
    catalog member share every operation."""
    n_closes = window + 1

    def compute(db: Session, symbol: str, instrument_id: UUID,
                sessions: list[date]) -> dict[date, float]:
        if not sessions:
            return {}
        end = max(sessions)
        closes: dict[date, Decimal] = {}
        for r in db.execute(text(
                "SELECT bar_date, close FROM market.price_bars_daily "
                "WHERE instrument_id = :iid AND source = :src "
                "  AND close IS NOT NULL AND bar_date <= :end"),
                {"iid": instrument_id, "src": VENDOR_SOURCE, "end": end}):
            closes[r.bar_date] = Decimal(r.close)
        if not closes:
            return {}
        all_splits: list[Split] = [
            Split(symbol=symbol, action_date=r.action_date,
                  ratio=Decimal(r.ratio))
            for r in db.execute(text(
                "SELECT action_date, ratio FROM market.corporate_actions "
                "WHERE instrument_id = :iid AND action_type = 'split' "
                "  AND action_date <= :end ORDER BY action_date"),
                {"iid": instrument_id, "end": end})]

        out: dict[date, float] = {}
        for t in sorted(sessions):
            cal = trading_days_between(
                "US", t - timedelta(days=_CAL_SLACK_DAYS), t)
            if len(cal) < n_closes or cal[-1] != t:
                continue            # not a US session / calendar too young
            win = cal[-n_closes:]
            if any(d not in closes for d in win):
                continue            # gap in the window: fail closed
            bars = [Bar(symbol=symbol, bar_date=d, open=closes[d],
                        high=closes[d], low=closes[d], close=closes[d],
                        volume=0) for d in win]
            adj = adjust_for_splits(
                bars, [sp for sp in all_splits if sp.action_date <= t])
            series = [float(b.close) for b in adj]
            if any(c <= 0 for c in series):
                continue            # a nonpositive close: fail closed
            rets = [series[i] / series[i - 1] - 1.0
                    for i in range(1, len(series))]
            out[t] = -(statistics.pstdev(rets) * math.sqrt(ANNUALIZATION))
        return out

    return compute


def vol_extent(db: Session, symbols: list[str], end: date) -> dict[str, object]:
    """The input-data extent hashed into dataset_version — the same inputs as
    momentum (vendor closes + splits <= END), declared locally (docstring)."""
    per_symbol: dict[str, object] = {}
    for symbol in symbols:
        bars = db.execute(text(
            "SELECT min(pb.bar_date) AS lo, max(pb.bar_date) AS hi, "
            "       count(*) AS n "
            "FROM market.price_bars_daily pb "
            "JOIN market.instruments i ON i.id = pb.instrument_id "
            "WHERE i.symbol = :s AND pb.source = :src "
            "  AND pb.close IS NOT NULL AND pb.bar_date <= :end"),
            {"s": symbol, "src": VENDOR_SOURCE, "end": end}).one()
        splits = db.execute(text(
            "SELECT count(*) AS n, max(ca.action_date) AS hi "
            "FROM market.corporate_actions ca "
            "JOIN market.instruments i ON i.id = ca.instrument_id "
            "WHERE i.symbol = :s AND ca.action_type = 'split' "
            "  AND ca.action_date <= :end"),
            {"s": symbol, "end": end}).one()
        per_symbol[symbol] = {
            "bars": {"min": bars.lo, "max": bars.hi, "rows": int(bars.n)},
            "splits": {"rows": int(splits.n), "max": splits.hi}}
    return {"source": VENDOR_SOURCE, "symbols": per_symbol}
