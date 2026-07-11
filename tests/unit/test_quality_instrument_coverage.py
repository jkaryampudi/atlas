"""Per-instrument gate coverage (rules v1.1): one instrument's bars must never
mask another instrument's missing history (review finding)."""
from datetime import date
from decimal import Decimal

from atlas.dcp.market_data.models import Bar, GateStatus
from atlas.dcp.market_data.quality import evaluate_gate


def _bar(sym: str, d: date) -> Bar:
    return Bar(symbol=sym, bar_date=d, open=Decimal(100), high=Decimal(101),
               low=Decimal(99), close=Decimal(100), volume=1000)


D = date(2024, 7, 15)


def test_missing_instrument_turns_day_red():
    gate = evaluate_gate(market="US", as_of=D, expected_days=[D],
                         bars_by_day={D: [_bar("AVGO", D)]},
                         expected_symbols=frozenset({"AVGO", "SPY"}))
    assert gate.status is GateStatus.RED
    assert any("SPY" in r for r in gate.reasons)


def test_all_instruments_present_stays_green():
    gate = evaluate_gate(market="US", as_of=D, expected_days=[D],
                         bars_by_day={D: [_bar("AVGO", D), _bar("SPY", D)]},
                         expected_symbols=frozenset({"AVGO", "SPY"}))
    assert gate.status is GateStatus.GREEN


def test_no_expected_symbols_keeps_day_level_semantics():
    gate = evaluate_gate(market="US", as_of=D, expected_days=[D],
                         bars_by_day={D: [_bar("AVGO", D)]})
    assert gate.status is GateStatus.GREEN
