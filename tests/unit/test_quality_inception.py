"""Inception-aware expected symbols (rules v1.2, deep-history backfill).

A symbol is expected on day D only from its inception (earliest STORED bar)
onward: an unlisted instrument is not a data gap; a listed one missing IS.
Fail-closed edges are pinned here: a symbol with no inception at all (no stored
bars — the needs-backfill state) stays expected on every day, and omitting the
``inceptions`` map reproduces v1.1 byte-for-byte.
"""
from datetime import date
from decimal import Decimal

from atlas.dcp.market_data.models import Bar, GateStatus
from atlas.dcp.market_data.quality import RULES_VERSION, evaluate_gate


def _bar(sym: str, d: date) -> Bar:
    return Bar(symbol=sym, bar_date=d, open=Decimal(100), high=Decimal(101),
               low=Decimal(99), close=Decimal(100), volume=1000)


D1, D6, D8 = date(2024, 6, 3), date(2024, 6, 10), date(2024, 6, 12)
EXPECTED = frozenset({"A", "B"})
INCEPTIONS = {"A": D1, "B": D6}  # B lists on day 6; A from day 1


def test_rules_version_marker_is_bumped():
    assert RULES_VERSION == "1.2"


def test_pre_inception_symbol_is_not_expected():
    """Day 1-5 territory: B has not listed yet, so A alone keeps the day green."""
    gate = evaluate_gate(market="US", as_of=D1, expected_days=[D1],
                         bars_by_day={D1: [_bar("A", D1)]},
                         expected_symbols=EXPECTED, inceptions=INCEPTIONS)
    assert gate.status is GateStatus.GREEN
    assert gate.reasons == ()


def test_missing_after_inception_is_red_exactly_as_before():
    """Day 8: B appeared on days 6-7, so its absence is a REAL gap -> RED."""
    gate = evaluate_gate(market="US", as_of=D8, expected_days=[D8],
                         bars_by_day={D8: [_bar("A", D8)]},
                         expected_symbols=EXPECTED, inceptions=INCEPTIONS)
    assert gate.status is GateStatus.RED
    assert any("B" in r for r in gate.reasons)


def test_present_after_inception_is_green():
    gate = evaluate_gate(market="US", as_of=D6, expected_days=[D6],
                         bars_by_day={D6: [_bar("A", D6), _bar("B", D6)]},
                         expected_symbols=EXPECTED, inceptions=INCEPTIONS)
    assert gate.status is GateStatus.GREEN


def test_symbol_without_inception_stays_fail_closed_red():
    """No stored bars at all (needs-backfill state): absent from the inception
    map means expected on EVERY day — a new universe entry must red the gate
    until its deliberate backfill, never vanish from coverage."""
    gate = evaluate_gate(market="US", as_of=D1, expected_days=[D1],
                         bars_by_day={D1: [_bar("A", D1)]},
                         expected_symbols=frozenset({"A", "GHOST"}),
                         inceptions={"A": D1})
    assert gate.status is GateStatus.RED
    assert any("GHOST" in r for r in gate.reasons)


def test_day_before_any_inception_is_green_with_v12_note():
    """A whole day before ANY instrument listed (deep-window start) has zero
    bars and is still green: an unlisted instrument is not a data gap. The
    reason string documents the rule so the gate row is auditable."""
    d0 = date(2024, 5, 28)  # before both inceptions
    gate = evaluate_gate(market="US", as_of=d0, expected_days=[d0],
                         bars_by_day={d0: []},
                         expected_symbols=EXPECTED, inceptions=INCEPTIONS)
    assert gate.status is GateStatus.GREEN
    assert any("not a data gap" in r for r in gate.reasons)


def test_day_after_inception_with_zero_bars_is_still_red():
    """The v1.2 note must never fire once anything is listed: an empty day
    after A's inception is a real gap (and a stale feed)."""
    gate = evaluate_gate(market="US", as_of=D8, expected_days=[D8],
                         bars_by_day={D8: []},
                         expected_symbols=EXPECTED, inceptions=INCEPTIONS)
    assert gate.status is GateStatus.RED


def test_omitting_inceptions_reproduces_v11_semantics():
    """inceptions=None disables the filter: every expected symbol is expected
    on every day, so pre-listing B reds the day exactly as under v1.1."""
    gate = evaluate_gate(market="US", as_of=D1, expected_days=[D1],
                         bars_by_day={D1: [_bar("A", D1)]},
                         expected_symbols=EXPECTED)
    assert gate.status is GateStatus.RED
    assert any("B" in r for r in gate.reasons)


def test_self_referential_first_bar_edge_is_green_by_construction():
    """Documented v1.2 edge: inception derives from stored bars, so the first
    day a brand-new symbol ever stores a bar defines its inception — a vendor
    hole at the very start of its history is indistinguishable from a late
    listing and gates green. From the first stored bar onward (previous tests)
    the gate is as strict as ever."""
    gate = evaluate_gate(market="US", as_of=D6, expected_days=[D6],
                         bars_by_day={D6: [_bar("A", D6), _bar("B", D6)]},
                         expected_symbols=EXPECTED,
                         inceptions={"A": D1, "B": D6})  # B's own first bar = D6
    assert gate.status is GateStatus.GREEN
