"""F-008 regression: vendor earnings 'actuals' must not become historical facts
before they were knowable. Guards live at parse time (report_date > period end,
report_date <= receipt date) and at store time (belt-and-suspenders)."""
from __future__ import annotations

from datetime import date, datetime

from atlas.dcp.market_data.earnings_history import (
    EarningsSurprise,
    parse_earnings_history,
    store_surprises,
)


def _payload(history: dict) -> dict:
    return {"General": {"CurrencyCode": "USD"}, "Earnings": {"History": history}}


def _row(report_date: str, actual: str = "1.00", estimate: str = "0.90") -> dict:
    return {"reportDate": report_date, "epsActual": actual, "epsEstimate": estimate,
            "surprisePercent": "11.1", "beforeAfterMarket": "AfterMarket", "currency": "USD"}


def test_valid_report_after_period_and_before_receipt_is_admitted():
    p = _payload({"2026-03-31": _row("2026-04-20")})          # 3 weeks after Q end
    rows = parse_earnings_history(p, "OK", known_as_of=date(2026, 7, 15))
    assert len(rows) == 1
    assert rows[0].fiscal_period_end == date(2026, 3, 31)
    assert rows[0].report_date == date(2026, 4, 20)


def test_future_dated_actual_is_excluded():
    """The PVH case: report_date 2026-08-25 is AFTER the 2026-07-15 receipt date
    — a fabricated future 'actual'. It must not be stored."""
    p = _payload({"2026-06-30": _row("2026-08-25")})
    rows = parse_earnings_history(p, "PVH", known_as_of=date(2026, 7, 15))
    assert rows == []


def test_report_on_or_before_period_end_is_excluded():
    """An actual announced on/before the quarter it reports has ended is
    impossible — excluded regardless of receipt date."""
    for rd in ("2026-03-31", "2026-03-15"):
        p = _payload({"2026-03-31": _row(rd)})
        assert parse_earnings_history(p, "X", known_as_of=date(2026, 7, 15)) == []


def test_without_known_as_of_period_guard_still_applies_future_guard_relaxed():
    """When no receipt date is supplied (e.g. a pure re-parse), the future-date
    guard is not enforced but the period-end guard always is."""
    future = _payload({"2026-06-30": _row("2026-08-25")})
    assert len(parse_earnings_history(future, "X")) == 1        # future guard off
    bad_period = _payload({"2026-03-31": _row("2026-03-15")})
    assert parse_earnings_history(bad_period, "X") == []        # period guard on


def test_valid_and_invalid_rows_partition_correctly():
    p = _payload({
        "2026-03-31": _row("2026-04-20"),     # valid
        "2026-06-30": _row("2026-08-25"),     # future -> excluded
        "2025-12-31": _row("2025-12-20"),     # before period end -> excluded
    })
    rows = parse_earnings_history(p, "MIX", known_as_of=date(2026, 7, 15))
    assert [r.fiscal_period_end for r in rows] == [date(2026, 3, 31)]


class _NoWriteSession:
    """A session whose execute() must never be called (proves bad rows are
    skipped before any INSERT)."""
    def execute(self, *_a, **_k):
        raise AssertionError("store_surprises attempted to INSERT a guarded row")


def test_store_belt_and_suspenders_skips_lookahead_rows():
    future = EarningsSurprise(
        symbol="PVH", fiscal_period_end=date(2026, 6, 30), report_date=date(2026, 8, 25),
        eps_actual=None, eps_estimate=None, surprise_pct=None,
        before_after_market="AfterMarket", currency="USD")
    before_period = EarningsSurprise(
        symbol="X", fiscal_period_end=date(2026, 3, 31), report_date=date(2026, 3, 15),
        eps_actual=None, eps_estimate=None, surprise_pct=None,
        before_after_market=None, currency="USD")
    # neither reaches session.execute -> _NoWriteSession never raises
    inserted = store_surprises(_NoWriteSession(), "iid", [future, before_period],
                               fetched_at=datetime(2026, 7, 15, 12, 0), source="Test")
    assert inserted == 0
