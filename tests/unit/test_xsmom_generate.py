"""Pure parts of the xsmom production signal generation (ADR-0010 wiring):
the winner ranking (hand-derived golden), the calendar rebalance triggers,
and the desk shortlist merge — no database, FrozenClock-free (all inputs are
plain values; the pg suite covers the stateful paths)."""
from __future__ import annotations

from datetime import date

from atlas.dcp.backtest.xsmom_pit_run import winner_count
from atlas.dcp.signals.xsmom.generate import (
    RankedSignal,
    is_month_end_session,
    next_rebalance_session,
    rank_winners,
)
from atlas.ops.daily import merge_shortlist

# ---------------------------------------------------------------- the ranking

# Hand-derived: 12 eligible names -> winner_count(12) = max(10, 12//10) = 10.
# Sort by (-formation, symbol): KO .30 > AA .25 = AB .25 (alphabetical) >
# ZZ .20 > MM .15 > NN .10 > PP .05 > QQ .00 > RR -.05 > SS -.10; TT -.15 and
# UU -.20 fall outside the winner set.
_FORMATION = {"KO": 0.30, "AA": 0.25, "AB": 0.25, "ZZ": 0.20, "MM": 0.15,
              "NN": 0.10, "PP": 0.05, "QQ": 0.00, "RR": -0.05, "SS": -0.10,
              "TT": -0.15, "UU": -0.20}


def test_rank_winners_hand_derived_golden():
    got = rank_winners(_FORMATION)
    assert got == [
        RankedSignal("KO", 1, 0.30), RankedSignal("AA", 2, 0.25),
        RankedSignal("AB", 3, 0.25), RankedSignal("ZZ", 4, 0.20),
        RankedSignal("MM", 5, 0.15), RankedSignal("NN", 6, 0.10),
        RankedSignal("PP", 7, 0.05), RankedSignal("QQ", 8, 0.00),
        RankedSignal("RR", 9, -0.05), RankedSignal("SS", 10, -0.10)]


def test_rank_winners_tie_breaks_alphabetically():
    got = rank_winners({"BBB": 0.5, "AAA": 0.5, "CCC": 0.5})
    assert [(s.symbol, s.rank) for s in got] == [("AAA", 1), ("BBB", 2),
                                                 ("CCC", 3)]


def test_winner_set_uses_the_approved_decile_rule():
    """The winner-set size is IMPORTED from xsmom_pit_run (the approved run):
    max(TOP_N, n_eligible // 10) — never a local restatement."""
    assert winner_count(200) == 20
    assert winner_count(105) == 10          # floor at TOP_N
    assert winner_count(3) == 10            # thin universe: hold what exists
    assert len(rank_winners({f"S{i:03d}": float(i) for i in range(200)})) == 20
    assert len(rank_winners({"A": 0.1, "B": 0.2})) == 2   # never pads


def test_rank_winners_empty_universe():
    assert rank_winners({}) == []


# ----------------------------------------------------------- calendar trigger

def test_month_end_session_facts():
    assert is_month_end_session(date(2026, 7, 31))        # Fri, last July session
    assert not is_month_end_session(date(2026, 7, 15))
    assert not is_month_end_session(date(2026, 7, 30))
    # 2026-12-31 is XNYS's last 2026 session (2027-01-01 holiday)
    assert is_month_end_session(date(2026, 12, 31))


def test_next_rebalance_session_is_strictly_after():
    assert next_rebalance_session(date(2026, 7, 15)) == date(2026, 7, 31)
    # from the month-end itself: the FOLLOWING month's last session
    assert next_rebalance_session(date(2026, 7, 31)) == date(2026, 8, 31)
    assert next_rebalance_session(date(2026, 8, 31)) == date(2026, 9, 30)


# ------------------------------------------------------- desk shortlist merge

def test_merge_shortlist_signals_lead_and_dedupe():
    assert merge_shortlist(["B", "A"], ["X", "A", "Y"]) == ["B", "A", "X", "Y"]
    assert merge_shortlist([], ["X", "Y"]) == ["X", "Y"]
    assert merge_shortlist(["B"], []) == ["B"]
    assert merge_shortlist(["B", "B", "A"], ["B"]) == ["B", "A"]
