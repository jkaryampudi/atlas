"""PEAD / SUE signal unit tests (pure, no DB).

The load-bearing invariants the adversarial audit will hammer:
  * golden SUE — hand-derived standardization on a built fixture;
  * STRUCTURAL no-look-ahead — a report dated after the decision session cannot
    change the signal at that session (flip it wildly; ranking byte-identical);
  * staleness-window boundary — 63 sessions fresh, 64 sessions stale;
  * split safety — a split between reports must not manufacture a phantom SUE;
  * eligibility — fewer than 4 prior quarters => no live signal;
  * after-market boundary — an after-market print is knowable the next session.
"""
from __future__ import annotations

import math
import statistics
from datetime import date, timedelta
from decimal import Decimal

from atlas.dcp.backtest.portfolio import PanelView, PricePanel
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.market_data.earnings_history import EarningsSurprise
from atlas.dcp.market_data.models import Split
from atlas.dcp.signals.pead.v1 import (
    STALENESS_SESSIONS,
    STANDARDIZE_MIN,
    build_earnings_view,
    compute_sue_reports,
    effective_index,
    pead_eligible,
)

SESSIONS = trading_days_between("US", date(2019, 1, 2), date(2020, 6, 30))


def _report(sym: str, i: int, actual: float, estimate: float, *,
            report_date: date, when: str | None = "BeforeMarket",
            surprise_pct: float | None = None) -> EarningsSurprise:
    return EarningsSurprise(
        symbol=sym, fiscal_period_end=date(2015, 1, 1) + timedelta(days=91 * i),
        report_date=report_date, eps_actual=Decimal(str(actual)),
        eps_estimate=Decimal(str(estimate)),
        surprise_pct=Decimal(str(surprise_pct)) if surprise_pct is not None else None,
        before_after_market=when)


def _quarterly(sym: str, actuals: list[float], estimate: float = 1.00,
               *, when: str | None = "BeforeMarket") -> list[EarningsSurprise]:
    """Reports spaced one per ~quarter, report_date = period end + 20 days."""
    out = []
    for i, a in enumerate(actuals):
        fpe = date(2016, 3, 31) + timedelta(days=91 * i)
        out.append(_report(sym, i, a, estimate, report_date=fpe + timedelta(days=20),
                           when=when))
    return out


# ---------------------------------------------------------------------------
# Golden SUE — hand-derived
# ---------------------------------------------------------------------------

def test_sue_golden_hand_derived():
    # estimate 1.00 throughout; actuals give surprises 0.10,-0.05,0.20,0.00,0.15
    # then the current 0.30. SUE_current = 0.30 / sample_stdev(prior five).
    reports = _quarterly("ZG", [1.10, 0.95, 1.20, 1.00, 1.15, 1.30])
    series = compute_sue_reports(reports, [])

    # hand arithmetic (independent of the production code):
    priors = [0.10, -0.05, 0.20, 0.00, 0.15]
    mean = sum(priors) / len(priors)                       # 0.08
    ss = sum((x - mean) ** 2 for x in priors)              # 0.043
    sd = math.sqrt(ss / (len(priors) - 1))                 # sample stdev, n-1
    expected = 0.30 / sd
    assert abs(series[-1].sue - expected) < 1e-12
    # and it matches the stdlib sample stdev exactly
    assert abs(series[-1].sue - 0.30 / statistics.stdev(priors)) < 1e-12
    # the split-adjusted surprise of the current report is the raw surprise
    # (no splits) — 0.30 up to float noise
    assert abs(series[-1].adj_surprise - 0.30) < 1e-9


def test_sue_undefined_below_min_priors():
    # 3 priors + current => current has only 3 priors (< STANDARDIZE_MIN=4)
    reports = _quarterly("ZM", [1.10, 0.95, 1.20, 1.30])
    series = compute_sue_reports(reports, [])
    assert STANDARDIZE_MIN == 4
    assert [s.sue for s in series[:STANDARDIZE_MIN]] == [None, None, None, None]
    # add one more prior => the 5th report (index 4) now has 4 priors and a SUE
    reports2 = _quarterly("ZM", [1.10, 0.95, 1.20, 1.00, 1.30])
    series2 = compute_sue_reports(reports2, [])
    assert series2[4].sue is not None


def test_sue_zero_stdev_is_undefined():
    # identical priors => zero dispersion => SUE undefined (never divide by zero)
    reports = _quarterly("ZZ", [1.10, 1.10, 1.10, 1.10, 1.30])
    series = compute_sue_reports(reports, [])
    assert series[-1].sue is None


# ---------------------------------------------------------------------------
# Split safety — a split between reports must not create a phantom SUE
# ---------------------------------------------------------------------------

def test_split_safety_sue_is_split_invariant():
    # Version A: five reports, one basis, economic surprises
    #   0.10, 0.12, 0.08, 0.11, current 0.20 (estimate 1.00).
    econ_actuals = [1.10, 1.12, 1.08, 1.11, 1.20]
    a = _quarterly("ZA", econ_actuals)
    sue_a = compute_sue_reports(a, [])[-1].sue
    assert sue_a is not None

    # Version B: identical economics, but the first four reports were filed
    # PRE-split in pre-split $/share (2x), a 2:1 split lands before the current
    # report, and the current report is post-split. Adjustment must divide the
    # four priors by 2, restoring Version A exactly.
    split_date = a[4].report_date - timedelta(days=10)     # just before current
    b = [
        _report("ZB", i, 2 * act, 2 * 1.00, report_date=a[i].report_date)
        for i, act in enumerate(econ_actuals[:4])
    ] + [_report("ZB", 4, 1.20, 1.00, report_date=a[4].report_date)]
    split = [Split(symbol="ZB", action_date=split_date, ratio=Decimal(2))]
    sue_b = compute_sue_reports(b, split)[-1].sue
    assert sue_b is not None
    assert abs(sue_b - sue_a) < 1e-12          # split is invisible to SUE

    # control: WITHOUT the split adjustment, B is a phantom — the pre-split 2x
    # surprises inflate the stdev and change the SUE materially
    sue_b_raw = compute_sue_reports(b, [])[-1].sue
    assert abs(sue_b_raw - sue_a) > 0.1


# ---------------------------------------------------------------------------
# Structural no-look-ahead
# ---------------------------------------------------------------------------

def _one_symbol_panel(sym: str) -> PricePanel:
    n = len(SESSIONS)
    closes = {sym: [100.0 + i for i in range(n)]}
    opens = {sym: [100.0 + i for i in range(n)]}
    return PricePanel(dates=list(SESSIONS), opens=opens, closes=closes)


def test_no_lookahead_future_report_cannot_move_signal_at_t():
    sym = "ZF"
    panel = _one_symbol_panel(sym)
    dates = panel.dates
    t = 200

    # reports: four early priors, one report knowable AT t (report_date = dates[t-5]),
    # and one report dated strictly AFTER t (in the future relative to the decision).
    priors = [
        _report(sym, 0, 1.10, 1.00, report_date=dates[20]),
        _report(sym, 1, 1.05, 1.00, report_date=dates[40]),
        _report(sym, 2, 1.20, 1.00, report_date=dates[60]),
        _report(sym, 3, 0.95, 1.00, report_date=dates[80]),
    ]
    live = _report(sym, 4, 1.30, 1.00, report_date=dates[t - 5])
    future = _report(sym, 5, 1.40, 1.00, report_date=dates[t + 10])
    reports = priors + [live, future]

    view_before = build_earnings_view({sym: reports}, {}, dates)
    sig_before = view_before.live(sym, t)
    assert sig_before is not None

    # FLIP the future report's numbers wildly (huge loss instead of a beat)
    future_flipped = _report(sym, 5, -99.0, 1.00, report_date=dates[t + 10])
    view_after = build_earnings_view({sym: priors + [live, future_flipped]}, {}, dates)
    sig_after = view_after.live(sym, t)

    assert sig_after == sig_before             # byte-identical at t
    # and the eligible/ranking at t is unchanged
    pv = PanelView(panel, t)
    assert pead_eligible(pv, view_before) == pead_eligible(pv, view_after)


# ---------------------------------------------------------------------------
# Staleness-window boundary — 63 sessions fresh, 64 stale
# ---------------------------------------------------------------------------

def _staleness_view(sym: str, dates: list[date], live_report_idx: int):
    priors = [
        _report(sym, 0, 1.10, 1.00, report_date=dates[5]),
        _report(sym, 1, 1.05, 1.00, report_date=dates[10]),
        _report(sym, 2, 1.20, 1.00, report_date=dates[15]),
        _report(sym, 3, 0.95, 1.00, report_date=dates[20]),
    ]
    live = _report(sym, 4, 1.30, 1.00, report_date=dates[live_report_idx])
    return build_earnings_view({sym: priors + [live]}, {}, dates)


def test_staleness_boundary_63_fresh_64_stale():
    sym = "ZS"
    dates = list(SESSIONS)
    t = 200
    assert STALENESS_SESSIONS == 63

    # BeforeMarket => effective_index == report_date's session index.
    fresh = _staleness_view(sym, dates, live_report_idx=t - 63)   # age exactly 63
    stale = _staleness_view(sym, dates, live_report_idx=t - 64)   # age 64

    assert fresh.live(sym, t) is not None
    assert stale.live(sym, t) is None


# ---------------------------------------------------------------------------
# After-market boundary — knowable the NEXT session
# ---------------------------------------------------------------------------

def test_after_market_boundary_effective_index():
    dates = list(SESSIONS)
    d = dates[100]
    assert effective_index(dates, d, "BeforeMarket") == 100      # same session
    assert effective_index(dates, d, "AfterMarket") == 101       # next session
    assert effective_index(dates, d, None) == 101                # unknown => next


def test_after_market_report_not_live_until_next_session():
    sym = "ZW"
    dates = list(SESSIONS)
    priors = [
        _report(sym, 0, 1.10, 1.00, report_date=dates[5]),
        _report(sym, 1, 1.05, 1.00, report_date=dates[10]),
        _report(sym, 2, 1.20, 1.00, report_date=dates[15]),
        _report(sym, 3, 0.95, 1.00, report_date=dates[20]),
    ]
    live_am = _report(sym, 4, 1.30, 1.00, report_date=dates[100], when="AfterMarket")
    view = build_earnings_view({sym: priors + [live_am]}, {}, dates)
    # at the report_date's own session (100) the after-market print is NOT yet known
    assert view.live(sym, 100) is None
    # it becomes live the next session (101)
    assert view.live(sym, 101) is not None
