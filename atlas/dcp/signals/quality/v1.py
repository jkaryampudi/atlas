"""Quality / gross-profitability signal v1 — Novy-Marx GP/A, ranked into a
top-decile long portfolio.

TEXTBOOK, ZERO PARAMETER SEARCH (mirroring momentum's and PEAD's discipline).
Novy-Marx 2013, "The Other Side of Value: The Gross Profitability Premium",
Journal of Financial Economics 108(1): gross profits-to-assets is the quality
dimension of value —

    GP/A_i = (trailing four quarters of grossProfit) / (most recent totalAssets)

both from QUARTERLY statements. All pinned parameters are textbook or
structural, not searched:
  * TRAILING_QUARTERS = 4 — the numerator is one fiscal year of gross profit;
  * the denominator is the NEWEST trailing quarter's totalAssets (no fallback
    to an older balance sheet);
  * STALENESS_SESSIONS = 252 — a GP/A older than an annual cycle without a
    fresh filing goes stale (structural: quality is slow-moving and the
    original paper uses ANNUAL data; a name that has not filed in over a year
    carries no current signal). NOT searched;
  * CONSECUTIVE_SPAN_DAYS = 300 — the four trailing quarters must be
    CONSECUTIVE: their period ends span three quarter-steps (~273 days); a
    skipped quarter forces the span to four steps (~365 days). 300 cleanly
    separates the two regimes — a structural consecutiveness test (the
    numerator must be exactly one year of gross profit), not a tuned knob.

FAIL-CLOSED ELIGIBILITY (no fallback, missing is missing):
  * all four trailing quarters must carry grossProfit — a missing grossProfit
    is NEVER derived from totalRevenue minus a cost line;
  * the newest quarter must carry totalAssets > 0 (a zero/negative printed
    total-assets figure is vendor noise, not a divisible denominator);
  * the four quarters must not MIX reporting currencies (the ratio is unitless
    only within a single currency). An unknown (NULL) currency is not a
    mismatch: a live probe (JPM, quarter filed 2026-07-14) found the vendor
    stamps currency_symbol LATE on release-stage rows, and the append-only
    fact store freezes that NULL forever — a strict rule would permanently
    blank the name over metadata, so only two DIFFERENT non-null currencies
    refuse;
  * the MOST RECENT knowable quarter governs: if its GP/A is undefined the
    name is ineligible — no fallback to an older, complete quarter (the spec
    is 'the most recent', exactly PEAD's rule).

--- NO LOOK-AHEAD IS STRUCTURAL (what the adversarial audit will hammer) ---

1. filing_date is the ONLY date the figures become knowable
   (market.quarterly_fundamentals stores no row without one strictly after the
   period end — a live probe found the vendor stamps filing_date = period end
   on a large minority of quarters, dropped fail-closed at ingestion). A
   quarter's GP/A uses four filings; it is knowable at the LATEST of them.

2. EFFECTIVE PANEL INDEX: the first session STRICTLY AFTER that latest
   filing_date (effective_index = bisect_right). Filings land after the close
   or intraday and carry no timing flag, so — conservatively, like PEAD's
   after-market rule — a filing on day D is first actionable the NEXT session.

3. At decision session t the live signal reads ONLY events with
   effective_index <= t (FundamentalsView.live). An event with
   effective_index > t is physically excluded — the accessor cannot see it.

4. GP/A at quarter i depends only on quarters at or before i, and every input
   filing of an event with effective_index <= t is itself <= t. Therefore no
   filing dated after t can change any signal visible at t: flipping a future
   quarter's numbers wildly leaves the ranking at t byte-identical — pinned by
   a structural test.

UNIVERSE NOTE (honesty, from the module that ranks with this signal):
Novy-Marx 2013 EXCLUDES financial firms — banks and insurers hold big
low-gross-margin balance sheets and have structurally low GP/A. This module
computes the textbook signal on WHATEVER universe it is handed; the
point-in-time runner runs the FULL S&P 500 by default and documents that the
original paper excludes financials. A financials-excluded run is a SECOND
registered trial behind an explicit flag, never a silent default.

Portfolio mechanics are IDENTICAL to signals.xsmom / signals.pead (rank
descending, top decile equal-weight, monthly, deterministic symbol tie-break)
so the ONLY difference versus momentum/PEAD is the signal — see
atlas/dcp/backtest/quality_pit_run.py.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date

from atlas.dcp.backtest.portfolio import PanelView
from atlas.dcp.market_data.quarterly_fundamentals import QuarterlyFundamentals

TRAILING_QUARTERS = 4      # numerator: one fiscal year of gross profit
STALENESS_SESSIONS = 252   # an annual cycle without a fresh filing -> stale
CONSECUTIVE_SPAN_DAYS = 300  # 3 quarter-steps ~273d vs a gapped ~365d

SPEC: dict[str, object] = {
    "family": "quality-gpa", "name": "novy_marx_gpa_top_decile",
    "version": "1.0.0",
    "signal": "GP/A (trailing-4Q gross profit / most recent total assets)",
    "trailing_quarters": TRAILING_QUARTERS,
    "staleness_sessions": STALENESS_SESSIONS,
    "consecutive_span_days": CONSECUTIVE_SPAN_DAYS,
    "missing_metrics": "fail-closed: grossProfit never derived from revenue "
                       "minus a cost line; newest-quarter totalAssets > 0 "
                       "required; no fallback to an older quarter",
    "filing_boundary": "figures knowable at the LATEST filing_date among the "
                       "four input quarters; tradable the NEXT session "
                       "(effective_index = bisect_right — filings land after "
                       "the close or intraday, treated conservatively)",
    "weighting": "equal", "rebalance": "monthly",
    "provenance": "textbook (Novy-Marx 2013 GP/A); no search"}


# ---------------------------------------------------------------------------
# GP/A computation (pure)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GpaQuarter:
    fiscal_period_end: date
    filing_date: date        # this quarter's own filing (anchor of THIS row)
    knowable_date: date      # max filing_date across the trailing window
    gpa: float | None        # None => incomplete window / bad assets / mixed ccy
    gross_profit_ttm: float | None
    total_assets: float | None


def compute_gpa_series(rows: list[QuarterlyFundamentals]) -> list[GpaQuarter]:
    """Chronological GP/A series for one instrument. For each stored quarter i
    the trailing window is the TRAILING_QUARTERS most recent stored quarters
    ending at i; GP/A is defined iff the window has exactly TRAILING_QUARTERS
    rows, they are CONSECUTIVE (period-end span <= CONSECUTIVE_SPAN_DAYS), all
    carry grossProfit, their known (non-null) currencies agree, and the newest
    carries totalAssets > 0. Undefined quarters still yield events (with
    gpa=None): the most recent knowable quarter governs eligibility — no
    fallback."""
    if not rows:
        return []
    rows = sorted(rows, key=lambda r: r.fiscal_period_end)
    out: list[GpaQuarter] = []
    for i, r in enumerate(rows):
        window = rows[max(0, i - TRAILING_QUARTERS + 1):i + 1]
        knowable = max(w.filing_date for w in window)
        gpa: float | None = None
        gp_ttm: float | None = None
        assets: float | None = None
        if (len(window) == TRAILING_QUARTERS
                and (window[-1].fiscal_period_end
                     - window[0].fiscal_period_end).days <= CONSECUTIVE_SPAN_DAYS
                and all(w.gross_profit is not None for w in window)
                and len({w.currency for w in window
                         if w.currency is not None}) <= 1
                and r.total_assets is not None and r.total_assets > 0):
            gp_ttm = float(sum(w.gross_profit for w in window
                               if w.gross_profit is not None))
            assets = float(r.total_assets)
            gpa = gp_ttm / assets
        out.append(GpaQuarter(
            fiscal_period_end=r.fiscal_period_end, filing_date=r.filing_date,
            knowable_date=knowable, gpa=gpa, gross_profit_ttm=gp_ttm,
            total_assets=assets))
    return out


# ---------------------------------------------------------------------------
# Point-in-time signal view (structural no-look-ahead on the panel calendar)
# ---------------------------------------------------------------------------

def effective_index(dates: list[date], filing_date: date) -> int:
    """First panel session at which the filing's figures are actionable: the
    session STRICTLY AFTER filing_date (bisect_right — filings land after the
    close or intraday; conservative, like PEAD's after-market rule). Returns
    len(dates) when the info arrives only after the panel ends."""
    return bisect_right(dates, filing_date)


@dataclass(frozen=True)
class SignalEvent:
    effective_index: int
    knowable_date: date      # the governing (latest-input) filing date
    gpa: float | None


class FundamentalsView:
    """Read-only per-symbol signal events, sorted by effective_index. live()
    returns the most-recent-knowable-and-fresh GP/A at session t, or None
    (ineligible) — structural no-look-ahead: events with effective_index > t
    are never consulted."""

    __slots__ = ("_events",)

    def __init__(self, events: dict[str, list[SignalEvent]]) -> None:
        self._events = events

    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._events))

    def live(self, symbol: str, t: int) -> float | None:
        """GP/A of the MOST RECENT quarter knowable by session t (its
        effective_index <= t) provided it is within STALENESS_SESSIONS of t;
        None when there is no such quarter, it is stale, or its GP/A is
        undefined (incomplete window / bad assets / mixed currency). No
        fallback to an older quarter — the spec is 'the most recent'."""
        evs = self._events.get(symbol)
        if not evs:
            return None
        lo, hi = 0, len(evs)
        while lo < hi:                       # rightmost event with eff_index <= t
            mid = (lo + hi) // 2
            if evs[mid].effective_index <= t:
                lo = mid + 1
            else:
                hi = mid
        if lo == 0:
            return None
        ev = evs[lo - 1]
        if t - ev.effective_index > STALENESS_SESSIONS:
            return None
        return ev.gpa


def build_fundamentals_view(rows: dict[str, list[QuarterlyFundamentals]],
                            dates: list[date]) -> FundamentalsView:
    """Build the point-in-time FundamentalsView for a panel calendar: per
    symbol, compute the GP/A series, map each quarter to the effective panel
    index of its GOVERNING (latest-input) filing date, drop quarters whose
    info arrives only after the panel ends, and sort by
    (effective_index, knowable_date)."""
    events: dict[str, list[SignalEvent]] = {}
    n = len(dates)
    for symbol, qrows in rows.items():
        series = compute_gpa_series(qrows)
        evs: list[SignalEvent] = []
        for gq in series:
            eff = effective_index(dates, gq.knowable_date)
            if eff >= n:
                continue  # knowable only after the panel ends — never in-window
            evs.append(SignalEvent(effective_index=eff,
                                   knowable_date=gq.knowable_date, gpa=gq.gpa))
        if evs:
            evs.sort(key=lambda e: (e.effective_index, e.knowable_date))
            events[symbol] = evs
    return FundamentalsView(events)


def quality_eligible(view: PanelView,
                     fundamentals: FundamentalsView) -> list[str]:
    """Signal-formation eligibility (the analogue of xsmom's eligible_symbols):
    a price at t (tradable) AND a live, fresh, defined GP/A. Membership is
    layered on by the point-in-time runner, exactly as xsmom_pit/pead_pit do."""
    t = view.t
    return [s for s in view.symbols()
            if view.close(s, t) is not None
            and fundamentals.live(s, t) is not None]
