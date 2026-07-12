"""Per-instrument gate coverage (rules v1.2; introduced in v1.1): one
instrument's bars must never mask another LISTED instrument's missing history
(review finding). v1.2 adds inception awareness — every case here is asserted
both without an inception map (fail-closed, the v1.1 behaviour) and with all
symbols incepted before the gated day, proving inception filtering never
weakens coverage for listed instruments."""
from datetime import date
from decimal import Decimal

from atlas.dcp.market_data.models import Bar, GateStatus
from atlas.dcp.market_data.quality import evaluate_gate


def _bar(sym: str, d: date) -> Bar:
    return Bar(symbol=sym, bar_date=d, open=Decimal(100), high=Decimal(101),
               low=Decimal(99), close=Decimal(100), volume=1000)


D = date(2024, 7, 15)
# both symbols listed well before the gated day: under v1.2 they are expected
LISTED = {"AVGO": date(2024, 7, 1), "SPY": date(2024, 7, 1)}


def test_missing_instrument_turns_day_red():
    gate = evaluate_gate(market="US", as_of=D, expected_days=[D],
                         bars_by_day={D: [_bar("AVGO", D)]},
                         expected_symbols=frozenset({"AVGO", "SPY"}))
    assert gate.status is GateStatus.RED
    assert any("SPY" in r for r in gate.reasons)


def test_missing_listed_instrument_turns_day_red_under_v12():
    """Same intent as above with the inception map supplied: SPY is LISTED
    (incepted before D), so its absence is a real hole — v1.2 must not let
    AVGO's bars mask it."""
    gate = evaluate_gate(market="US", as_of=D, expected_days=[D],
                         bars_by_day={D: [_bar("AVGO", D)]},
                         expected_symbols=frozenset({"AVGO", "SPY"}),
                         inceptions=LISTED)
    assert gate.status is GateStatus.RED
    assert any("SPY" in r for r in gate.reasons)


def test_all_instruments_present_stays_green():
    gate = evaluate_gate(market="US", as_of=D, expected_days=[D],
                         bars_by_day={D: [_bar("AVGO", D), _bar("SPY", D)]},
                         expected_symbols=frozenset({"AVGO", "SPY"}))
    assert gate.status is GateStatus.GREEN


def test_all_instruments_present_stays_green_under_v12():
    gate = evaluate_gate(market="US", as_of=D, expected_days=[D],
                         bars_by_day={D: [_bar("AVGO", D), _bar("SPY", D)]},
                         expected_symbols=frozenset({"AVGO", "SPY"}),
                         inceptions=LISTED)
    assert gate.status is GateStatus.GREEN


def test_no_expected_symbols_keeps_day_level_semantics():
    gate = evaluate_gate(market="US", as_of=D, expected_days=[D],
                         bars_by_day={D: [_bar("AVGO", D)]})
    assert gate.status is GateStatus.GREEN
