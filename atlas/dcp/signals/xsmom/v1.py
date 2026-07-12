"""Cross-sectional momentum v1 — the classic 12-1 relative-strength portfolio:
rank the eligible universe by total return over sessions [t-252, t-21] (twelve
months excluding the most recent one) and hold the top 10 equal-weight.

Textbook parameters, NO parameter search: Jegadeesh & Titman (1993), "Returns
to Buying Winners and Selling Losers: Implications for Stock Market
Efficiency", Journal of Finance 48(1) — the 12-month formation period with the
most recent month skipped (the skip avoids the one-month reversal they
document; cf. Asness 1994). 252/21 sessions are the standard trading-day
equivalents of 12/1 months; monthly rebalancing is their holding convention.
With ~100 eligible names, the top 10 IS the winner decile; equal weight within
the winner portfolio is their construction. Long-only per the Atlas mandate
(no loser short leg).

Point-in-time listing honesty: a name enters the ranking only once it has at
least SEASONING (=252) prior sessions of data — late listings join when
seasoned, never retroactively. If fewer than TOP_N names are eligible the
portfolio holds fewer, equal-weighted 1/n_selected; it never pads. Ties rank
alphabetically (deterministic).

Same code path serves backtest and production (ADR-0002 #4).
"""
from __future__ import annotations

from atlas.dcp.backtest.portfolio import PanelView

LOOKBACK, SKIP = 252, 21
TOP_N, SEASONING = 10, 252
SPEC: dict[str, object] = {"family": "xsmom", "name": "jt_12_1_top10",
    "version": "1.0.0", "lookback_sessions": LOOKBACK, "skip_sessions": SKIP,
    "top_n": TOP_N, "seasoning_sessions": SEASONING, "weighting": "equal",
    "rebalance": "monthly",
    "provenance": "textbook (Jegadeesh & Titman 1993, 12-1 momentum); no search"}


def eligible_symbols(view: PanelView) -> list[str]:
    """Symbols with a price at t AND >= SEASONING prior sessions of data.
    Under the panel's contiguity invariant, a close at t - SEASONING proves
    exactly that history. Shared verbatim with the null model (the monkey
    draws from THIS set), so strategy and null face identical universes."""
    t = view.t
    return [s for s in view.symbols()
            if view.close(s, t) is not None
            and view.close(s, t - SEASONING) is not None]


def xsmom_v1(view: PanelView) -> dict[str, float]:
    """Target weights at rebalance t: top TOP_N by 12-1 return, equal weight."""
    t = view.t
    ranked: list[tuple[float, str]] = []
    for s in eligible_symbols(view):
        c_form = view.close(s, t - LOOKBACK)
        c_skip = view.close(s, t - SKIP)
        # contiguity: both exist for any eligible symbol (SEASONING == LOOKBACK)
        assert c_form is not None and c_skip is not None
        ranked.append((c_skip / c_form - 1.0, s))
    ranked.sort(key=lambda rs: (-rs[0], rs[1]))
    top = ranked[:TOP_N]
    if not top:
        return {}
    w = 1.0 / len(top)
    return {s: w for _, s in top}
