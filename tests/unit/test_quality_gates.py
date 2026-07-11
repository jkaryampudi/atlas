from datetime import date
from decimal import Decimal

from atlas.dcp.market_data.models import Bar, GateStatus
from atlas.dcp.market_data.quality import evaluate_gate


def _bar(sym: str, d: date, close: str) -> Bar:
    c = Decimal(close)
    return Bar(symbol=sym, bar_date=d, open=c, high=c, low=c, close=c, volume=1000)


def test_missing_day_is_red():
    days = [date(2026, 7, 9), date(2026, 7, 10)]
    bars = {date(2026, 7, 9): [_bar("SPY", date(2026, 7, 9), "500")]}
    g = evaluate_gate(market="US", as_of=date(2026, 7, 10), expected_days=days,
                      bars_by_day=bars)
    assert g.status is GateStatus.RED


def test_unexplained_big_move_is_amber():
    d1, d2 = date(2026, 7, 9), date(2026, 7, 10)
    bars = {d1: [_bar("XYZ", d1, "100")], d2: [_bar("XYZ", d2, "160")]}
    g = evaluate_gate(market="US", as_of=d2, expected_days=[d1, d2], bars_by_day=bars)
    assert g.status is GateStatus.AMBER


def test_explained_move_stays_green():
    d1, d2 = date(2026, 7, 9), date(2026, 7, 10)
    bars = {d1: [_bar("XYZ", d1, "100")], d2: [_bar("XYZ", d2, "160")]}
    g = evaluate_gate(market="US", as_of=d2, expected_days=[d1, d2], bars_by_day=bars,
                      explained_symbols=frozenset({"XYZ"}))
    assert g.status is GateStatus.GREEN
