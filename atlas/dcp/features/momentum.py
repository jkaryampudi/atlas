"""momentum_12_1 — the xsmom formation return as a stored point-in-time
feature: close[t-SKIP] / close[t-LOOKBACK] - 1 on split-adjusted closes,
knowable at session t's close.

THE MATH IS IMPORTED, NOT REWRITTEN (ADR-0011 step 1). The pinned parameters
come from signals/xsmom/v1 (LOOKBACK=252, SKIP=21; seasoning == LOOKBACK: a
name needs a close 252 sessions back, so eligibility is implied by the
window) and the split adjustment from market_data/adjustment — both files are
part of this feature's code_sha, so a change to either invalidates the
definition until reviewed. The arithmetic replicates the production ranker
(signals/xsmom/generate._formation_returns) OPERATION FOR OPERATION so the
equivalence tests can demand byte-identical floats: Decimal vendor closes ->
adjust_for_splits (Decimal division) -> float() each leg -> float division
c_skip / c_form - 1.0.

ELIGIBILITY (fail-closed, the ranker's exact rule): a value exists at session
t iff the instrument has a vendor close on EVERY of the WINDOW=253 US
sessions ending at t (contiguity — a gappy series is never scored) and the
formation close is positive. Sessions failing the rule are simply absent.

NO LOOK-AHEAD, structurally: closes are probed at dates <= t by construction
(the window ends at t) and only splits with action_date <= t are applied — a
split recorded for a later date cannot reach the value at t, mirroring the
ranker's query cap.

DATASET_VERSION EXTENT (see store.py for the hash): per symbol, over the
inputs actually readable by END —
  bars:   min(bar_date), max(bar_date), row count of vendor closes <= END
          (source = EodhdAdapter, close IS NOT NULL);
  splits: row count and max(action_date) of split actions <= END.
Rows dated after END never enter the extent (they cannot change any value
knowable by END); any new or backfilled row <= END re-versions the dataset.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.dcp.market_data.adjustment import adjust_for_splits
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.market_data.models import Bar, Split
from atlas.dcp.signals.xsmom.v1 import LOOKBACK, SKIP

VENDOR_SOURCE = "EodhdAdapter"      # same vendor-bar pin as the ranker
WINDOW = LOOKBACK + 1               # 253 sessions ending at t (contiguity)
_SKIP_IDX = WINDOW - 1 - SKIP       # index of t-SKIP inside the window
_CAL_SLACK_DAYS = 550               # calendar days that always cover 253 sessions


def compute_momentum(db: Session, symbol: str, instrument_id: UUID,
                     sessions: list[date]) -> dict[date, float]:
    """{session: formation return} for every target session where the
    contiguity rule holds. Point-in-time per session: the window ends at t
    and only splits known by t are applied."""
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
        Split(symbol=symbol, action_date=r.action_date, ratio=Decimal(r.ratio))
        for r in db.execute(text(
            "SELECT action_date, ratio FROM market.corporate_actions "
            "WHERE instrument_id = :iid AND action_type = 'split' "
            "  AND action_date <= :end ORDER BY action_date"),
            {"iid": instrument_id, "end": end})]

    out: dict[date, float] = {}
    for t in sorted(sessions):
        cal = trading_days_between(
            "US", t - timedelta(days=_CAL_SLACK_DAYS), t)
        if len(cal) < WINDOW or cal[-1] != t:
            continue                    # not a US session / calendar too young
        window = cal[-WINDOW:]
        if any(d not in closes for d in window):
            continue                    # gap in the window: fail closed
        probe = (window[0], window[_SKIP_IDX], window[-1])  # t-252, t-21, t
        bars = [Bar(symbol=symbol, bar_date=d, open=closes[d], high=closes[d],
                    low=closes[d], close=closes[d], volume=0) for d in probe]
        adj = adjust_for_splits(
            bars, [sp for sp in all_splits if sp.action_date <= t])
        c_form, c_skip = float(adj[0].close), float(adj[1].close)
        if c_form <= 0:
            continue                    # unpriceable base: fail closed
        out[t] = c_skip / c_form - 1.0
    return out


def momentum_extent(db: Session, symbols: list[str],
                    end: date) -> dict[str, object]:
    """The input-data extent hashed into dataset_version (module docstring)."""
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
            "  AND ca.action_date <= :end"), {"s": symbol, "end": end}).one()
        per_symbol[symbol] = {
            "bars": {"min": bars.lo, "max": bars.hi, "rows": int(bars.n)},
            "splits": {"rows": int(splits.n), "max": splits.hi}}
    return {"source": VENDOR_SOURCE, "symbols": per_symbol}
