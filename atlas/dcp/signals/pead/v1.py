"""PEAD / earnings-surprise signal v1 — Standardized Unexpected Earnings (SUE),
the Foster-Olsen-Shevlin form, ranked into a top-decile long portfolio.

TEXTBOOK, ZERO PARAMETER SEARCH (mirroring momentum's discipline). The one
orthogonal factor an external review asked for: not momentum, but the clean,
point-in-time-backtestable cousin of estimate revisions — earnings SURPRISE and
its post-announcement drift (PEAD; Ball & Brown 1968, Bernard & Thomas 1989;
SUE per Foster, Olsen & Shevlin 1984, "Earnings Releases, Anomalies, and the
Behavior of Security Returns", The Accounting Review).

    SUE_i = (epsActual_i - epsEstimate_i) / stdev( surprise over the prior 8
             reported quarters )

The numerator is the current report's surprise; the denominator standardizes it
by the dispersion of the firm's own recent surprises. All three pinned
parameters are textbook, not searched:
  * STANDARDIZE_WINDOW = 8 prior quarters for the standardization (two years);
  * STANDARDIZE_MIN    = 4 prior quarters required, else the name is ineligible
    that period (we do NOT silently fall back to a raw surprise);
  * STALENESS_SESSIONS = 63 (~one quarter) drift-capture window: PEAD decays
    over roughly a quarter, so a report older than that carries no live signal.

--- NO LOOK-AHEAD IS STRUCTURAL (what the adversarial audit will hammer) ---

1. The report_date is the ONLY date the surprise becomes knowable. Each report
   is mapped to an EFFECTIVE PANEL INDEX — the first session at which a trader
   could act on it:
       BeforeMarket        -> the session of report_date (known by its close);
       AfterMarket/unknown  -> the NEXT session (the print lands after the close,
                              first actionable the following session).
   (effective_index below: bisect_left vs bisect_right on the panel calendar.)

2. At decision session t the live signal reads ONLY events with
   effective_index <= t (EarningsView.live). An event with effective_index > t
   is physically excluded — the accessor cannot see it.

3. SUE_i depends only on reports STRICTLY PRIOR to i (its standardization
   window), and every prior of a report i with effective_index_i <= t has an
   effective_index <= effective_index_i <= t. Therefore no report dated after t
   can change any signal visible at t. Flipping a future report's numbers
   wildly leaves the ranking at t byte-identical — pinned by a structural test.

--- SPLIT SAFETY ---

epsActual/epsEstimate are per-share and SPLIT-SENSITIVE. A single report's
surprise is internally consistent at report time, but the rolling stdev mixes
reports that may straddle a split. Before differencing/standardizing, every EPS
value is put on a common split-adjusted basis using the HOUSE split convention
(market_data.adjustment.adjust_for_splits, keyed on report_date — a value
reported strictly before a split's action_date is divided by the ratio, exactly
as a pre-split price bar is). A split OUTSIDE a report's window scales all of
its reports equally and cancels in the ratio; a split INSIDE the window
rescales the pre-split surprises correctly, so a split can never manufacture a
phantom SUE — pinned by a split-safety test.

surprisePercent (the vendor's split-neutral ratio) is offered as a SECONDARY
variant for the adversarial cross-check; SUE is the primary signal.

Portfolio mechanics are IDENTICAL to signals.xsmom (rank descending, top decile
equal-weight, monthly, deterministic symbol tie-break) so the ONLY difference
versus momentum is the signal — see atlas/dcp/backtest/pead_pit_run.py.
"""
from __future__ import annotations

import statistics
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from atlas.dcp.backtest.portfolio import PanelView
from atlas.dcp.market_data.adjustment import adjust_for_splits
from atlas.dcp.market_data.earnings_history import EarningsSurprise
from atlas.dcp.market_data.models import Bar, Split

STANDARDIZE_WINDOW = 8   # prior quarters over which surprises are standardized
STANDARDIZE_MIN = 4      # min prior quarters, else the name is ineligible
STALENESS_SESSIONS = 63  # drift-capture window (~one quarter); textbook, not searched
VARIANTS = ("sue", "surprise_pct")

SPEC: dict[str, object] = {
    "family": "pead-sue", "name": "foster_olsen_shevlin_sue_top_decile",
    "version": "1.0.0", "signal": "SUE (standardized unexpected earnings)",
    "standardize_window_quarters": STANDARDIZE_WINDOW,
    "standardize_min_quarters": STANDARDIZE_MIN,
    "staleness_sessions": STALENESS_SESSIONS,
    "eps_basis": "split-adjusted on read (house adjust_for_splits, keyed on "
                 "report_date)",
    "after_market_boundary": "after-market/unknown prints are tradable the next "
                             "session (effective_index = bisect_right)",
    "weighting": "equal", "rebalance": "monthly",
    "provenance": "textbook (Foster-Olsen-Shevlin SUE; PEAD); no search"}


# ---------------------------------------------------------------------------
# SUE computation (pure; split-safe standardization)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SueReport:
    fiscal_period_end: date
    report_date: date
    before_after_market: str | None
    adj_surprise: float          # split-adjusted (actual - estimate), common basis
    sue: float | None            # None => fewer than STANDARDIZE_MIN priors / zero stdev
    surprise_pct: float | None   # vendor split-neutral ratio (secondary variant)


def _split_reciprocals(symbol: str, dates: list[date],
                       splits: list[Split]) -> dict[date, Decimal]:
    """Reciprocal split factor per report_date, computed by running the HOUSE
    helper (adjust_for_splits) on unit-price probe bars. adjust_for_splits
    divides a bar dated strictly before a split by the ratio, so a unit probe
    at date D comes back as 1/factor(D) — exactly the reciprocal we multiply an
    EPS by to put it on the post-split basis. Reusing the canonical helper (not
    a re-implementation) means the EPS adjustment shares the bars' convention by
    construction."""
    if not splits:
        return {}
    probes = [Bar(symbol=symbol, bar_date=d, open=Decimal(1), high=Decimal(1),
                  low=Decimal(1), close=Decimal(1), volume=0)
              for d in sorted(set(dates))]
    return {b.bar_date: b.close for b in adjust_for_splits(probes, splits)}


def compute_sue_reports(reports: list[EarningsSurprise],
                        splits: list[Split]) -> list[SueReport]:
    """Chronological SUE series for one instrument. Each report's EPS legs are
    split-adjusted to a common basis (keyed on report_date), differenced, and
    standardized by the sample stdev of the up-to-8 immediately prior surprises;
    fewer than STANDARDIZE_MIN priors (or a zero stdev) leaves SUE undefined."""
    if not reports:
        return []
    reports = sorted(reports, key=lambda r: (r.fiscal_period_end, r.report_date))
    recip = _split_reciprocals(reports[0].symbol,
                               [r.report_date for r in reports], splits)
    adj: list[float] = []
    for r in reports:
        f = recip.get(r.report_date, Decimal(1))
        adj.append(float(r.eps_actual * f - r.eps_estimate * f))
    out: list[SueReport] = []
    for i, r in enumerate(reports):
        priors = adj[max(0, i - STANDARDIZE_WINDOW):i]
        sue: float | None = None
        if len(priors) >= STANDARDIZE_MIN:
            sd = statistics.stdev(priors)
            if sd > 0:
                sue = adj[i] / sd
        out.append(SueReport(
            fiscal_period_end=r.fiscal_period_end, report_date=r.report_date,
            before_after_market=r.before_after_market, adj_surprise=adj[i],
            sue=sue,
            surprise_pct=(float(r.surprise_pct)
                          if r.surprise_pct is not None else None)))
    return out


# ---------------------------------------------------------------------------
# Point-in-time signal view (structural no-look-ahead on the panel calendar)
# ---------------------------------------------------------------------------

def effective_index(dates: list[date], report_date: date,
                    when: str | None) -> int:
    """First panel session at which the surprise is knowable/actionable.
    BeforeMarket -> the report_date's own session (bisect_left, = its index if
    a session); AfterMarket/unknown -> the next session (bisect_right). Returns
    len(dates) when the info arrives only after the panel ends."""
    return (bisect_left(dates, report_date) if when == "BeforeMarket"
            else bisect_right(dates, report_date))


@dataclass(frozen=True)
class SignalEvent:
    effective_index: int
    report_date: date
    sue: float | None
    surprise_pct: float | None


class EarningsView:
    """Read-only per-symbol signal events, sorted by effective_index. live()
    returns the most-recent-knowable-and-fresh signal at session t, or None
    (ineligible) — structural no-look-ahead: events with effective_index > t
    are never consulted."""

    __slots__ = ("_events",)

    def __init__(self, events: dict[str, list[SignalEvent]]) -> None:
        self._events = events

    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._events))

    def live(self, symbol: str, t: int, *, variant: str = "sue") -> float | None:
        """Signal value of the MOST RECENT report knowable by session t (its
        effective_index <= t) provided that report is within STALENESS_SESSIONS
        of t; None when there is no such report, it is stale, or its value for
        the requested variant is undefined (e.g. SUE with < 4 priors). No
        fallback to an older report — the spec is 'the most recent'."""
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
        return ev.sue if variant == "sue" else ev.surprise_pct


def build_earnings_view(reports: dict[str, list[EarningsSurprise]],
                        splits: dict[str, list[Split]],
                        dates: list[date]) -> EarningsView:
    """Build the point-in-time EarningsView for a panel calendar: per symbol,
    compute the SUE series, map each report to its effective panel index, drop
    reports whose info arrives only after the panel ends, and sort by
    (effective_index, report_date)."""
    events: dict[str, list[SignalEvent]] = {}
    n = len(dates)
    for symbol, rows in reports.items():
        series = compute_sue_reports(rows, splits.get(symbol, []))
        evs: list[SignalEvent] = []
        for sr in series:
            eff = effective_index(dates, sr.report_date, sr.before_after_market)
            if eff >= n:
                continue  # knowable only after the panel ends — never in-window
            evs.append(SignalEvent(effective_index=eff, report_date=sr.report_date,
                                   sue=sr.sue, surprise_pct=sr.surprise_pct))
        if evs:
            evs.sort(key=lambda e: (e.effective_index, e.report_date))
            events[symbol] = evs
    return EarningsView(events)


def pead_eligible(view: PanelView, earnings: EarningsView, *,
                  variant: str = "sue") -> list[str]:
    """Signal-formation eligibility (the analogue of xsmom's eligible_symbols):
    a price at t (tradable) AND a live, fresh, defined signal. Membership is
    layered on by the point-in-time runner, exactly as xsmom_pit does."""
    t = view.t
    return [s for s in view.symbols()
            if view.close(s, t) is not None
            and earnings.live(s, t, variant=variant) is not None]
