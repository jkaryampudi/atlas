"""Pure parts of the PEAD/SUE production signal generation (ADR-0013/0014
wiring): the winner ranking (hand-derived golden), the shared decile rule, the
alphabetical tie-break — no database (the pg suite covers the stateful paths).
The calendar rebalance triggers are IMPORTED verbatim from xsmom.generate, so
they are pinned once in test_xsmom_generate and never restated here."""
from __future__ import annotations

from atlas.dcp.backtest.xsmom_pit_run import winner_count
from atlas.dcp.signals.pead.generate import RankedPeadSignal, rank_pead_winners

# Hand-derived: 12 eligible names -> winner_count(12) = max(10, 12//10) = 10.
# Sort by (-sue, symbol): KO 3.0 > AA 2.5 = AB 2.5 (alphabetical) > ZZ 2.0 >
# MM 1.5 > NN 1.0 > PP 0.5 > QQ 0.0 > RR -0.5 > SS -1.0; TT -1.5 and UU -2.0
# fall outside the winner set (identical structure to the momentum golden — the
# ONLY difference between the two sleeves is the ranked value's provenance).
_SUE = {"KO": 3.0, "AA": 2.5, "AB": 2.5, "ZZ": 2.0, "MM": 1.5, "NN": 1.0,
        "PP": 0.5, "QQ": 0.0, "RR": -0.5, "SS": -1.0, "TT": -1.5, "UU": -2.0}


def test_rank_pead_winners_hand_derived_golden():
    got = rank_pead_winners(_SUE)
    assert got == [
        RankedPeadSignal("KO", 1, 3.0), RankedPeadSignal("AA", 2, 2.5),
        RankedPeadSignal("AB", 3, 2.5), RankedPeadSignal("ZZ", 4, 2.0),
        RankedPeadSignal("MM", 5, 1.5), RankedPeadSignal("NN", 6, 1.0),
        RankedPeadSignal("PP", 7, 0.5), RankedPeadSignal("QQ", 8, 0.0),
        RankedPeadSignal("RR", 9, -0.5), RankedPeadSignal("SS", 10, -1.0)]


def test_rank_pead_winners_tie_breaks_alphabetically():
    got = rank_pead_winners({"BBB": 0.5, "AAA": 0.5, "CCC": 0.5})
    assert [(s.symbol, s.rank) for s in got] == [("AAA", 1), ("BBB", 2),
                                                 ("CCC", 3)]


def test_pead_winner_set_uses_the_shared_approved_decile_rule():
    """The winner-set size is IMPORTED from xsmom_pit_run — the SAME decile rule
    momentum was approved on, so the two sleeves can never diverge on it:
    max(TOP_N, n_eligible // 10)."""
    assert winner_count(200) == 20
    assert winner_count(105) == 10          # floor at TOP_N
    assert winner_count(3) == 10            # thin universe: hold what exists
    assert len(rank_pead_winners({f"S{i:03d}": float(i) for i in range(200)})) == 20
    assert len(rank_pead_winners({"A": 0.1, "B": 0.2})) == 2   # never pads


def test_rank_pead_winners_empty_universe():
    assert rank_pead_winners({}) == []
