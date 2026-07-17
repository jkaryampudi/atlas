"""Quality / GP/A signal unit tests (pure, no DB).

The load-bearing invariants the adversarial audit will hammer:
  * golden GP/A — hand-derived trailing-4Q sum over newest assets;
  * STRUCTURAL no-look-ahead — a filing dated after the decision session cannot
    change the signal at that session (flip it wildly; ranking byte-identical);
  * filing-day boundary — a quarter filed on session d is live the NEXT
    session (bisect_right; filings land after the close or intraday);
  * 4-quarter completeness — 3 stored quarters, or a missing grossProfit in
    the window, leaves GP/A undefined (missing is missing, never derived);
  * consecutiveness — a skipped quarter stretches the window past
    CONSECUTIVE_SPAN_DAYS and blanks the signal (the numerator must be one
    year of gross profit);
  * no fallback — the MOST RECENT knowable quarter governs: if its GP/A is
    undefined the name is ineligible even when an older quarter was complete;
  * staleness boundary — 252 sessions fresh, 253 stale;
  * degenerate denominators — totalAssets <= 0 or absent => undefined;
  * currency consistency — a mixed-currency window => undefined.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from atlas.dcp.backtest.portfolio import PanelView, PricePanel
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.market_data.quarterly_fundamentals import QuarterlyFundamentals
from atlas.dcp.signals.quality.v1 import (
    CONSECUTIVE_SPAN_DAYS,
    STALENESS_SESSIONS,
    TRAILING_QUARTERS,
    build_fundamentals_view,
    compute_gpa_series,
    effective_index,
    quality_eligible,
)

SESSIONS = trading_days_between("US", date(2019, 1, 2), date(2021, 12, 31))

Q_ENDS = [date(2018, 3, 31), date(2018, 6, 30), date(2018, 9, 30),
          date(2018, 12, 31), date(2019, 3, 31), date(2019, 6, 30),
          date(2019, 9, 30), date(2019, 12, 31)]


def _q(sym: str, fpe: date, *, gp: float | None, ta: float | None = None,
       filing: date | None = None, currency: str | None = "USD",
       tr: float | None = None) -> QuarterlyFundamentals:
    return QuarterlyFundamentals(
        symbol=sym, fiscal_period_end=fpe,
        filing_date=filing if filing is not None else fpe + timedelta(days=40),
        gross_profit=Decimal(str(gp)) if gp is not None else None,
        total_revenue=Decimal(str(tr)) if tr is not None else None,
        total_assets=Decimal(str(ta)) if ta is not None else None,
        currency=currency)


def _series(sym: str, gps: list[float | None], *, assets: float | None = 250.0,
            ends: list[date] | None = None) -> list[QuarterlyFundamentals]:
    """Quarterly rows; only the NEWEST row carries total_assets (the spec's
    denominator is the newest quarter's balance sheet)."""
    ends = ends if ends is not None else Q_ENDS[:len(gps)]
    return [_q(sym, fpe, gp=gp, ta=assets if i == len(gps) - 1 else 100.0)
            for i, (fpe, gp) in enumerate(zip(ends, gps))]


# ---------------------------------------------------------------------------
# Golden GP/A — hand-derived
# ---------------------------------------------------------------------------

def test_gpa_golden_hand_derived():
    # trailing four quarters of gross profit: 11.0 + 12.5 + 10.5 + 14.0 = 48.0
    # newest total assets: 250.0  ->  GP/A = 48.0 / 250.0 = 0.192 exactly
    rows = _series("ZG", [9.0, 11.0, 12.5, 10.5, 14.0], assets=250.0)
    series = compute_gpa_series(rows)
    assert series[-1].gpa is not None
    assert abs(series[-1].gpa - (11.0 + 12.5 + 10.5 + 14.0) / 250.0) < 1e-12
    assert abs(series[-1].gpa - 0.192) < 1e-12
    assert series[-1].gross_profit_ttm == 48.0
    assert series[-1].total_assets == 250.0
    # the 9.0 quarter is OUTSIDE the trailing window — moving it must not matter
    rows2 = _series("ZG", [99999.0, 11.0, 12.5, 10.5, 14.0], assets=250.0)
    assert compute_gpa_series(rows2)[-1].gpa == series[-1].gpa
    # knowable date = the LATEST filing among the four inputs (= newest here)
    assert series[-1].knowable_date == rows[-1].filing_date


def test_gpa_knowable_at_latest_input_filing_not_newest_quarter():
    # a LATE filing on an OLDER in-window quarter governs knowability
    rows = _series("ZL", [10.0, 10.0, 10.0, 10.0, 10.0])
    late = _q("ZL", rows[2].fiscal_period_end, gp=10.0, ta=100.0,
              filing=rows[-1].filing_date + timedelta(days=30))
    rows[2] = late
    series = compute_gpa_series(rows)
    assert series[-1].gpa is not None
    assert series[-1].knowable_date == late.filing_date


# ---------------------------------------------------------------------------
# Completeness / consecutiveness / denominators / currency — all fail-closed
# ---------------------------------------------------------------------------

def test_gpa_undefined_below_four_quarters():
    assert TRAILING_QUARTERS == 4
    series = compute_gpa_series(_series("ZM", [11.0, 12.5, 10.5]))
    assert [s.gpa for s in series] == [None, None, None]
    series4 = compute_gpa_series(_series("ZM", [11.0, 12.5, 10.5, 14.0]))
    assert series4[-1].gpa is not None


def test_gpa_missing_gross_profit_in_window_is_undefined_never_derived():
    # one in-window quarter lacks grossProfit but HAS totalRevenue — GP/A must
    # be undefined (missing is missing; no revenue-minus-cost derivation)
    rows = _series("ZD", [11.0, None, 10.5, 14.0])
    rows[1] = _q("ZD", Q_ENDS[1], gp=None, tr=50.0, ta=100.0)
    assert compute_gpa_series(rows)[-1].gpa is None


def test_gpa_skipped_quarter_blanks_the_signal():
    # 2018-12-31 missing from storage: the trailing four stored rows span
    # 2018-03-31 .. 2019-03-31 = 365 days > CONSECUTIVE_SPAN_DAYS -> undefined
    ends = [Q_ENDS[0], Q_ENDS[1], Q_ENDS[2], Q_ENDS[4]]
    assert (ends[-1] - ends[0]).days > CONSECUTIVE_SPAN_DAYS
    series = compute_gpa_series(_series("ZC", [11.0, 12.5, 10.5, 14.0], ends=ends))
    assert series[-1].gpa is None
    # the normal consecutive span sits comfortably inside the bound
    assert (Q_ENDS[3] - Q_ENDS[0]).days <= CONSECUTIVE_SPAN_DAYS


def test_gpa_missing_or_nonpositive_assets_is_undefined():
    assert compute_gpa_series(
        _series("ZA", [11.0, 12.5, 10.5, 14.0], assets=None))[-1].gpa is None
    assert compute_gpa_series(
        _series("ZA", [11.0, 12.5, 10.5, 14.0], assets=0.0))[-1].gpa is None
    assert compute_gpa_series(
        _series("ZA", [11.0, 12.5, 10.5, 14.0], assets=-5.0))[-1].gpa is None


def test_gpa_mixed_currency_window_is_undefined():
    rows = _series("ZX", [11.0, 12.5, 10.5, 14.0])
    rows[1] = _q("ZX", Q_ENDS[1], gp=12.5, ta=100.0, currency="EUR")
    assert compute_gpa_series(rows)[-1].gpa is None


def test_gpa_unknown_currency_is_not_a_mismatch():
    # a NULL currency is late vendor metadata, not a mismatch (live probe: JPM's
    # quarter filed 2026-07-14 arrived with currency_symbol NULL; the
    # append-only store freezes it) — the window stays DEFINED
    rows = _series("ZU", [11.0, 12.5, 10.5, 14.0])
    rows[-1] = _q("ZU", Q_ENDS[3], gp=14.0, ta=250.0, currency=None)
    series = compute_gpa_series(rows)
    assert series[-1].gpa is not None
    assert abs(series[-1].gpa - 0.192) < 1e-12
    # ... but two DIFFERENT known currencies still refuse
    rows[0] = _q("ZU", Q_ENDS[0], gp=11.0, ta=100.0, currency="EUR")
    assert compute_gpa_series(rows)[-1].gpa is None


def test_no_fallback_most_recent_quarter_governs():
    # quarter 4 (index 3) has a defined GP/A; quarter 5 loses its balance sheet
    # -> the LIVE signal after quarter 5's filing is None (no fallback)
    rows = _series("ZN", [11.0, 12.5, 10.5, 14.0])
    broken = _q("ZN", Q_ENDS[4], gp=12.0, ta=None)
    view = build_fundamentals_view({"ZN": rows + [broken]}, list(SESSIONS))
    t_ok = effective_index(list(SESSIONS), rows[-1].filing_date)
    t_broken = effective_index(list(SESSIONS), broken.filing_date)
    assert view.live("ZN", t_ok) is not None       # complete quarter live
    assert view.live("ZN", t_broken) is None       # newer broken quarter governs


# ---------------------------------------------------------------------------
# Filing-day boundary — live the NEXT session
# ---------------------------------------------------------------------------

def test_filing_day_boundary_effective_index():
    dates = list(SESSIONS)
    d = dates[100]
    # filed ON a session: actionable the NEXT session (after-close convention)
    assert effective_index(dates, d) == 101
    # filed on a Saturday: actionable the following Monday's session
    saturday = date(2019, 6, 8)
    assert saturday.weekday() == 5 and saturday not in dates
    assert dates[effective_index(dates, saturday)] == date(2019, 6, 10)


def test_filed_today_live_next_session():
    dates = list(SESSIONS)
    rows = _series("ZB", [11.0, 12.5, 10.5, 14.0])
    filed_on = dates[150]
    rows[-1] = _q("ZB", Q_ENDS[3], gp=14.0, ta=250.0, filing=filed_on)
    view = build_fundamentals_view({"ZB": rows}, dates)
    assert view.live("ZB", 150) is None            # filing lands after the close
    assert view.live("ZB", 151) is not None        # first actionable session


# ---------------------------------------------------------------------------
# Staleness boundary — 252 sessions fresh, 253 stale
# ---------------------------------------------------------------------------

def test_staleness_boundary_252_fresh_253_stale():
    assert STALENESS_SESSIONS == 252
    dates = list(SESSIONS)
    t = 400
    rows = _series("ZS", [11.0, 12.5, 10.5, 14.0])

    def _view_with_filing(idx: int):
        r = list(rows)
        # filing lands the session BEFORE dates[idx] so effective_index == idx
        r[-1] = _q("ZS", Q_ENDS[3], gp=14.0, ta=250.0, filing=dates[idx - 1])
        return build_fundamentals_view({"ZS": r}, dates)

    fresh = _view_with_filing(t - STALENESS_SESSIONS)      # age exactly 252
    stale = _view_with_filing(t - STALENESS_SESSIONS - 1)  # age 253
    assert fresh.live("ZS", t) is not None
    assert stale.live("ZS", t) is None


# ---------------------------------------------------------------------------
# Structural no-look-ahead
# ---------------------------------------------------------------------------

def _one_symbol_panel(sym: str) -> PricePanel:
    n = len(SESSIONS)
    closes = {sym: [100.0 + i for i in range(n)]}
    opens = {sym: [100.0 + i for i in range(n)]}
    return PricePanel(dates=list(SESSIONS), opens=opens, closes=closes)


def test_no_lookahead_future_filing_cannot_move_signal_at_t():
    sym = "ZF"
    panel = _one_symbol_panel(sym)
    dates = panel.dates
    t = 300

    rows = _series(sym, [11.0, 12.5, 10.5, 14.0, 12.0])
    # the newest quarter's filing lands strictly AFTER the decision session t
    future = _q(sym, Q_ENDS[5], gp=13.0, ta=300.0, filing=dates[t + 10])
    view_before = build_fundamentals_view({sym: rows + [future]}, dates)
    sig_before = view_before.live(sym, t)
    assert sig_before is not None

    # FLIP the future filing's numbers wildly (a catastrophic quarter)
    future_flipped = _q(sym, Q_ENDS[5], gp=-99999.0, ta=1.0, filing=dates[t + 10])
    view_after = build_fundamentals_view({sym: rows + [future_flipped]}, dates)
    sig_after = view_after.live(sym, t)

    assert sig_after == sig_before             # byte-identical at t
    # and the eligible set at t is unchanged
    pv = PanelView(panel, t)
    assert quality_eligible(pv, view_before) == quality_eligible(pv, view_after)
    # ... while AFTER the future filing lands, the flip of course shows
    assert view_before.live(sym, t + 11) != view_after.live(sym, t + 11)
