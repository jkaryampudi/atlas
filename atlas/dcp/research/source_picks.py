"""External source-pick tracking (investing.com etc.) — the MEASUREMENT layer
for "some picks out/underperform; learn why the underperformers do, and filter
toward the outperformers".

WALL + INVARIANT 2. An external pick is NOT a committee memo. A fabricated BUY
memo with no DCP evidence is a schema violation (Constitution 4), so external
picks never enter research.memos; they live in research.source_picks with:
  1. a point-in-time FEATURE SNAPSHOT (`snapshot_features`) — the substrate a
     future filter would learn from, computed only from data knowable at the
     recommendation date (adjusted closes, sector, earnings proximity, SPY
     regime, latest-<=-date fundamentals). Unrecoverable if not captured now.
  2. its OWN forward-return grading (`grade_picks`) — excess vs SPY at 20/60
     sessions, the scorecard's exact vindication rule (excess > 0 = the pick
     OUTperformed). This answers, per source, "does it beat a dartboard".

MEASURED, NEVER APPLIED (learning-loop doctrine). Nothing here reaches
sizing/pricing/execution — a pick is tracked and scored, never bridged. A
filter that skews the book toward outperformers is FUTURE work: it must clear
the same gauntlet (null model, deflated Sharpe, purged walk-forward) as any
factor and be Principal-signed. Most learned filters are expected to FAIL that
bar; this module only builds the honest dataset to test them on.

The desk MAY separately opine on a pick through the ordinary analyze path
(a real, evidence-grounded, source-tagged committee memo) — that is a distinct,
constitutional artifact, not this raw source-edge record.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.clock import Clock
from atlas.dcp.indicators.core import rsi, sma
from atlas.dcp.market_data.adjustment import adjust_for_splits
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.market_data.fundamentals import _get, _number
from atlas.dcp.market_data.models import Bar, Split
from atlas.dcp.scorecard import VENDOR_SOURCE, anchor_index, dartboard_baseline
from atlas.dcp.signals.regime.v1 import TREND_WINDOW, VOL_WINDOW, classify_series
from atlas.dcp.backtest.engine import OBar

PICK_FEATURE_VERSION = "v1"
# Excess-vs-SPY horizons in trading SESSIONS. 5/10 (~1-2 weeks) give the
# Principal early visibility; 20/60 (~1-3 months) are where signal separates
# from noise — a filter is validated on the longer ones (0034 docstring).
PICK_HORIZONS: tuple[int, ...] = (5, 10, 20, 60)
_MIN_REGIME_BARS = max(TREND_WINDOW, VOL_WINDOW + 1) + 1
_RET6 = Decimal("0.000001")                 # match the memo_outcomes excess quantum

# Point-in-time fundamentals captured into the snapshot (subset of the desk's
# whitelist, numeric only). Path tuples mirror fundamentals._STOCK_FACTS; a
# missing/unparseable value is a null feature (fail-soft, honest), never a
# fabricated number. Widening this set is a feature_version bump.
_FUND_FEATURES: tuple[tuple[str, tuple[str, str]], ...] = (
    ("market_cap", ("Highlights", "MarketCapitalization")),
    ("trailing_pe", ("Valuation", "TrailingPE")),
    ("forward_pe", ("Valuation", "ForwardPE")),
    ("ps_ttm", ("Valuation", "PriceSalesTTM")),
    ("ev_ebitda", ("Valuation", "EnterpriseValueEbitda")),
    ("revenue_growth_yoy", ("Highlights", "QuarterlyRevenueGrowthYOY")),
    ("operating_margin", ("Highlights", "OperatingMarginTTM")),
    ("profit_margin", ("Highlights", "ProfitMargin")),
    ("roe", ("Highlights", "ReturnOnEquityTTM")),
    ("dividend_yield", ("Highlights", "DividendYield")),
)


# ---------------------------------------------------------------------------
# Point-in-time price series (self-contained: no look-ahead past `through`)
# ---------------------------------------------------------------------------

def _adjusted_closes(session: Session, instrument_id: str,
                     through: date) -> list[tuple[date, float]]:
    """Ascending split-adjusted closes for an instrument, hard-capped at
    `through` (no look-ahead) and adjusted with ONLY splits recorded on or
    before `through` — the storage convention (raw bars, adjust on read).
    Same shape as scorecard._load_series, kept local so this measurement
    module owns its PIT boundary rather than depending on a private."""
    rows = session.execute(text(
        "SELECT bar_date, close FROM market.price_bars_daily "
        "WHERE instrument_id = :iid AND source = :src "
        "  AND close IS NOT NULL AND bar_date <= :d ORDER BY bar_date"),
        {"iid": instrument_id, "src": VENDOR_SOURCE, "d": through}).all()
    splits = [Split(symbol=str(instrument_id), action_date=r.action_date,
                    ratio=Decimal(r.ratio))
              for r in session.execute(text(
                  "SELECT action_date, ratio FROM market.corporate_actions "
                  "WHERE instrument_id = :iid AND action_type = 'split' "
                  "  AND action_date <= :d ORDER BY action_date"),
                  {"iid": instrument_id, "d": through}).all()]
    if not splits:
        return [(r.bar_date, float(r.close)) for r in rows]
    bars = [Bar(symbol=str(instrument_id), bar_date=r.bar_date, open=r.close,
                high=r.close, low=r.close, close=r.close, volume=0) for r in rows]
    return [(b.bar_date, float(b.close)) for b in adjust_for_splits(bars, splits)]


def _ret(closes: list[float], lag: int) -> float | None:
    if len(closes) <= lag or closes[-1 - lag] <= 0:
        return None
    return closes[-1] / closes[-1 - lag] - 1.0


# ---------------------------------------------------------------------------
# Context features (regime, sector, earnings proximity, fundamentals)
# ---------------------------------------------------------------------------

def _spy_regime(session: Session, on: date) -> str | None:
    """The deterministic P3 regime label on split-adjusted SPY closes as of the
    last SPY bar <= `on`; None below the warmup minimum (never a warmup
    artifact). Mirrors desk_context.extract_regime_evidence's PIT loading."""
    rows = session.execute(text(
        "SELECT pb.bar_date, pb.close FROM market.price_bars_daily pb "
        "JOIN market.instruments i ON i.id = pb.instrument_id "
        "WHERE i.symbol = 'SPY' AND pb.source = :src AND pb.bar_date <= :on "
        "ORDER BY pb.bar_date DESC LIMIT 252"), {"src": VENDOR_SOURCE, "on": on}).all()
    if len(rows) < _MIN_REGIME_BARS:
        return None
    rows = list(reversed(rows))
    as_of = rows[-1].bar_date
    splits = [Split(symbol="SPY", action_date=r.action_date, ratio=Decimal(r.ratio))
              for r in session.execute(text(
                  "SELECT ca.action_date, ca.ratio FROM market.corporate_actions ca "
                  "JOIN market.instruments i ON i.id = ca.instrument_id "
                  "WHERE i.symbol = 'SPY' AND ca.action_type = 'split' "
                  "  AND ca.action_date <= :on ORDER BY ca.action_date"),
                  {"on": as_of}).all()]
    bars = [Bar(symbol="SPY", bar_date=r.bar_date, open=r.close, high=r.close,
                low=r.close, close=r.close, volume=0) for r in rows]
    closes = [float(b.close) for b in adjust_for_splits(bars, splits)]
    return str(classify_series([OBar(open=c, high=c, low=c, close=c, volume=0.0)
                                for c in closes])[-1])


def _sessions_to_next_earnings(session: Session, instrument_id: str,
                               on: date) -> int | None:
    """XNYS sessions from `on` to the next known future earnings report; None
    when no future report is on the calendar (absence is not a signal). Only
    report_date > on is used — a report on/before `on` is already public.

    PIT CAVEAT (honest): market.earnings_calendar carries no as-of history, so
    this reads the calendar AS IT STANDS NOW. That is point-in-time-correct for
    the intended use — picks recorded at recommendation time, the monthly
    baseline going FORWARD — where `on` is ~today and the calendar reflects
    current knowledge. For BACKDATED ingestion it can see report dates added
    after `on`; do not trust this one feature on historically backfilled picks
    (the price/fundamentals features stay clean — fundamentals fail-safe to
    null when no snapshot predates `on`)."""
    nxt = session.execute(text(
        "SELECT min(report_date) FROM market.earnings_calendar "
        "WHERE instrument_id = :iid AND report_date > :on"),
        {"iid": instrument_id, "on": on}).scalar()
    if nxt is None:
        return None
    # inclusive of the report session, exclusive of `on`: sessions in (on, nxt].
    return max(0, len(trading_days_between("US", on, nxt)) - 1)


def _fundamentals(session: Session, instrument_id: str, on: date) -> dict[str, float | None]:
    """The latest fundamentals payload with as_of <= `on` (no look-ahead),
    reduced to the numeric feature subset. Every field fail-soft to None: a
    missing snapshot or unparseable value is honest absence, never a guess."""
    out: dict[str, float | None] = {k: None for k, _ in _FUND_FEATURES}
    row = session.execute(text(
        "SELECT payload FROM market.fundamentals WHERE instrument_id = :iid "
        "  AND as_of <= :on ORDER BY as_of DESC LIMIT 1"),
        {"iid": instrument_id, "on": on}).scalar()
    if row is None:
        return out
    payload = row if isinstance(row, dict) else json.loads(row)
    for key, path in _FUND_FEATURES:
        rendered = _number(_get(payload, path))
        if rendered is not None:
            try:
                out[key] = float(rendered)
            except ValueError:
                out[key] = None
    return out


# ---------------------------------------------------------------------------
# The snapshot
# ---------------------------------------------------------------------------

def snapshot_features(session: Session, instrument_id: str, symbol: str,
                      as_of_session: date) -> dict[str, object]:
    """The point-in-time feature dict for one pick, computed from ONLY data
    knowable at `as_of_session`. Price/technical features come from the
    instrument's own split-adjusted closes; context from sector, the SPY
    regime, next-earnings proximity, and latest-<=-date fundamentals. Every
    feature is deterministic and fail-soft to None (never fabricated)."""
    closes = [c for _, c in _adjusted_closes(session, instrument_id, as_of_session)]
    sma50 = sma(closes, 50)[-1] if len(closes) >= 50 else None
    sma200 = sma(closes, 200)[-1] if len(closes) >= 200 else None
    rsi14 = rsi(closes, 14)[-1] if len(closes) >= 15 else None
    last = closes[-1] if closes else None
    daily = [closes[i] / closes[i - 1] - 1.0
             for i in range(1, len(closes)) if closes[i - 1] > 0]
    vol20 = (statistics.pstdev(daily[-20:]) * math.sqrt(252)
             if len(daily) >= 20 else None)
    sector = session.execute(text(
        "SELECT sector_gics FROM market.instruments WHERE id = :iid"),
        {"iid": instrument_id}).scalar()

    feats: dict[str, object] = {
        "feature_version": PICK_FEATURE_VERSION,
        "n_closes": len(closes),
        # price / technical (formation return uses the production 21/252 skip)
        "mom_12_1": _ret(closes, 252) if len(closes) > 252 else None,
        "ret_20d": _ret(closes, 20),
        "ret_63d": _ret(closes, 63),
        "px_over_sma50": (last / sma50 - 1.0) if (last and sma50) else None,
        "px_over_sma200": (last / sma200 - 1.0) if (last and sma200) else None,
        "rsi_14": rsi14,
        "vol_20d_ann": vol20,
        # context
        "sector_gics": (sector or None),
        "sessions_to_next_earnings": _sessions_to_next_earnings(session, instrument_id, as_of_session),
        "spy_regime": _spy_regime(session, as_of_session),
    }
    feats.update(_fundamentals(session, instrument_id, as_of_session))
    return feats


# ---------------------------------------------------------------------------
# Record / grade / report
# ---------------------------------------------------------------------------

def record_pick(session: Session, *, source: str, ticker: str,
                instrument_id: str, recommendation_date: date,
                as_of_session: date, source_recommendation: str = "BUY") -> str | None:
    """Insert one pick with its PIT feature snapshot. Idempotent: a duplicate
    (source, ticker, recommendation_date) is a no-op returning None (the
    monthly ingest can safely re-run). Features are immutable once written."""
    feats = snapshot_features(session, instrument_id, ticker, as_of_session)
    return session.execute(text(
        "INSERT INTO research.source_picks "
        "(source, ticker, instrument_id, recommendation_date, as_of_session, "
        " source_recommendation, feature_version, features) "
        "VALUES (:s, :t, :iid, :rd, :ao, :rec, :fv, CAST(:f AS jsonb)) "
        "ON CONFLICT (source, ticker, recommendation_date) DO NOTHING "
        "RETURNING id"),
        {"s": source, "t": ticker, "iid": instrument_id, "rd": recommendation_date,
         "ao": as_of_session, "rec": source_recommendation,
         "fv": PICK_FEATURE_VERSION, "f": json.dumps(feats)}).scalar()


@dataclass(frozen=True)
class GradeReport:
    graded: int
    still_immature: int


def grade_picks(session: Session, clock: Clock) -> GradeReport:
    """Fill excess at every PICK_HORIZON for matured, ungraded picks —
    WRITE-ONCE (WHERE ... IS NULL, so a graded outcome is a fact, never
    revised). excess = pick_return - SPY_return over the pick's own priceable
    sessions from the recommendation anchor, the scorecard's rule (excess > 0 =
    OUTperformed). Fail-closed: a pick whose instrument or SPY series can't
    anchor is skipped, not guessed."""
    on = clock.now().date()
    spy_iid = session.execute(text(
        "SELECT id FROM market.instruments WHERE symbol = 'SPY' "
        "ORDER BY is_active DESC LIMIT 1")).scalar()
    spy = _adjusted_closes(session, spy_iid, on) if spy_iid is not None else []
    spy_dates = [d for d, _ in spy]
    graded = immature = 0
    cols = ", ".join(f"excess_{h}" for h in PICK_HORIZONS)
    null_any = " OR ".join(f"excess_{h} IS NULL" for h in PICK_HORIZONS)
    rows = session.execute(text(
        f"SELECT id, instrument_id, recommendation_date, {cols} "
        "FROM research.source_picks "
        f"WHERE ({null_any}) AND instrument_id IS NOT NULL")).all()
    for r in rows:
        series = _adjusted_closes(session, r.instrument_id, on)
        dates = [d for d, _ in series]
        a = anchor_index(dates, r.recommendation_date)
        sa = anchor_index(spy_dates, r.recommendation_date)
        if a is None or sa is None or series[a][1] <= 0 or spy[sa][1] <= 0:
            immature += 1
            continue
        updates: dict[str, Decimal] = {}
        for h in PICK_HORIZONS:
            col = f"excess_{h}"
            if getattr(r, col) is not None:
                continue
            if a + h >= len(series) or sa + h >= len(spy):
                immature += 1
                continue
            if series[a + h][1] <= 0 or spy[sa + h][1] <= 0:
                immature += 1
                continue
            pick_ret = Decimal(str(series[a + h][1] / series[a][1] - 1.0))
            spy_ret = Decimal(str(spy[sa + h][1] / spy[sa][1] - 1.0))
            excess = (pick_ret - spy_ret).quantize(_RET6)
            updates[col] = excess
        if updates:
            sets = ", ".join(f"{c} = :{c}" for c in updates)
            session.execute(text(
                f"UPDATE research.source_picks SET {sets}, graded_at = :ts "
                f"WHERE id = :id AND ({' OR '.join(c + ' IS NULL' for c in updates)})"),
                {**{c: v for c, v in updates.items()}, "ts": clock.now(), "id": r.id})
            graded += len(updates)
    return GradeReport(graded=graded, still_immature=immature)


@dataclass(frozen=True)
class SourceEdge:
    source: str
    horizon: int
    n_matured: int
    outperform_rate: float | None      # fraction with excess > 0
    dartboard: float | None            # base rate of the sign across ALL picks
    edge: float | None                 # rate - dartboard; the verdict


def source_edge_report(session: Session) -> list[SourceEdge]:
    """Per (source, horizon): did the source's picks OUTperform SPY more often
    than a dartboard would? `edge = outperform_rate - dartboard`; near-zero
    edge is the honest verdict that the source has no skill to filter (you
    cannot filter signal out of noise). Reuses scorecard.dartboard_baseline so
    the baseline rule lives in exactly one place."""
    out: list[SourceEdge] = []
    for source in [r.source for r in session.execute(text(
            "SELECT DISTINCT source FROM research.source_picks ORDER BY source")).all()]:
        for h in PICK_HORIZONS:
            col = f"excess_{h}"
            excesses = [Decimal(r.e) for r in session.execute(text(
                f"SELECT {col} AS e FROM research.source_picks "
                f"WHERE source = :s AND {col} IS NOT NULL"), {"s": source}).all()]
            n = len(excesses)
            if n == 0:
                out.append(SourceEdge(source, h, 0, None, None, None))
                continue
            rate = sum(1 for e in excesses if e > 0) / n
            # picks are BUY-shaped, so the dart is the base rate of excess > 0.
            dart = dartboard_baseline("BUY", excesses)
            dartf = float(dart) if dart is not None else None
            out.append(SourceEdge(source, h, n, rate, dartf,
                                  (rate - dartf) if dartf is not None else None))
    return out
