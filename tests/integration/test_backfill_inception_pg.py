"""Deep-history backfill under quality rules v1.2 (inception-aware coverage).

Multi-inception fixture scenario (tests/fixtures/inception): TINCA trades from
day 1 of its history (2024-06-03), TINCB lists on day 6 (2024-06-10), trades two
sessions, then goes missing on day 8 (2024-06-12). The window deliberately opens
BEFORE either inception (2024-05-27) — deep-backfill shape:

- sessions before any inception (05-28..05-31): GREEN with the documented v1.2
  note — an unlisted instrument is not a data gap;
- sessions where only TINCA is listed (06-03..06-07): GREEN without TINCB;
- sessions with both listed (06-10, 06-11): GREEN;
- sessions where LISTED TINCB is missing (06-12..06-14): honestly RED — the fix
  must not weaken the gate for real gaps.

Robust to shared-DB state by construction: any standard seed instruments other
suites may have committed carry stored bars from 2024-07-10 onward, so their
v1.2 inception excludes them from this June window — which is exactly the rule
under test. FixtureAdapter only; the real API is never touched here.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.adapters.fixture import FixtureAdapter
from atlas.dcp.market_data.backfill import backfill
from atlas.dcp.market_data.quality import inception_map
from tests.conftest import requires_pg

pytestmark = requires_pg
ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "inception"

START, END = date(2024, 5, 27), date(2024, 6, 14)     # opens before any inception
A_INCEPTION, B_INCEPTION = date(2024, 6, 3), date(2024, 6, 10)
B_MISSING = (date(2024, 6, 12), date(2024, 6, 13), date(2024, 6, 14))
PRE_INCEPTION_SESSIONS = (date(2024, 5, 28), date(2024, 5, 29),
                          date(2024, 5, 30), date(2024, 5, 31))


@pytest.fixture
def seeds(tmp_path):
    p = tmp_path / "inception_seeds.csv"
    p.write_text(
        "symbol,exchange,market,instrument_type,name,sector_gics,currency,economic_exposure\n"
        "TINCA,NYSE,US,stock,Test Inception A,Broad,USD,US\n"
        "TINCB,NASDAQ,US,stock,Test Inception B,Broad,USD,US\n")
    return p


def _clean_window(s) -> None:
    s.execute(text("DELETE FROM market.data_quality_gates "
                   "WHERE gate_date BETWEEN :a AND :b"), {"a": START, "b": END})
    s.execute(text("DELETE FROM market.fx_rates_daily "
                   "WHERE rate_date BETWEEN :a AND :b"), {"a": START, "b": END})


def _run(s, seeds, *, markets=("US",), start=START, end=END):
    audit = PostgresAuditLog(s, FrozenClock(
        datetime(end.year, end.month, end.day, 22, tzinfo=UTC)))
    return backfill(session=s, adapter=FixtureAdapter(FIXTURES), audit=audit,
                    markets=list(markets), start=start, end=end, seeds_csv=seeds)


def test_deep_window_multi_inception_gates(clean_audit, seeds):
    s = clean_audit
    _clean_window(s)
    report = _run(s, seeds)
    us = report.markets["US"]
    assert us.sessions == 14                      # XNYS 05-28..06-14 (05-27 holiday)
    assert us.bars == 12                          # TINCA 10 + TINCB 2
    assert us.amber == 0
    # RED exactly where LISTED TINCB is missing — and nowhere else
    assert us.red == len(B_MISSING)
    assert us.first_red == tuple(d.isoformat() for d in B_MISSING)
    assert report.failed                          # a real gap is a real failure
    # inception dates are reported per instrument
    assert us.inceptions["TINCA"] == A_INCEPTION
    assert us.inceptions["TINCB"] == B_INCEPTION

    gates = dict(s.execute(text(
        "SELECT gate_date, status FROM market.data_quality_gates "
        "WHERE market='US' AND gate_date BETWEEN :a AND :b"),
        {"a": START, "b": END}).all())
    for d in PRE_INCEPTION_SESSIONS:              # nothing listed yet: green
        assert gates[d] == "green"
    for d in (date(2024, 6, 3), date(2024, 6, 7)):  # A alone, before B lists
        assert gates[d] == "green"
    for d in (B_INCEPTION, date(2024, 6, 11)):    # both present
        assert gates[d] == "green"
    for d in B_MISSING:                           # B listed then missing: RED
        assert gates[d] == "red"

    # the pre-inception green is documented on the gate row itself (v1.2 note)
    note = s.execute(text("SELECT reasons FROM market.data_quality_gates "
                          "WHERE market='US' AND gate_date=:d"),
                     {"d": PRE_INCEPTION_SESSIONS[0]}).scalar()
    assert "not a data gap" in str(note)
    red_reason = s.execute(text("SELECT reasons FROM market.data_quality_gates "
                                "WHERE market='US' AND gate_date=:d"),
                           {"d": B_MISSING[0]}).scalar()
    assert "TINCB" in str(red_reason)


def test_deep_window_fx_series_expected_from_first_stored_rate(clean_audit, seeds):
    """FX analogue of inception: weekdays before the pair's first stored rate
    (2024-06-03) are not gaps even though the window opens 05-27; the fetched
    series itself is complete, so nothing is missing after inception."""
    s = clean_audit
    _clean_window(s)
    report = _run(s, seeds)
    fx = report.fx["USDAUD"]
    assert fx.rows == 10                          # weekdays 06-03..06-14
    assert fx.first_rate == A_INCEPTION           # series inception = first stored rate
    assert fx.missing_weekdays == 0               # 05-27..05-31 precede inception
    assert fx.empty is False


def test_fx_weekday_gap_after_inception_still_counts(clean_audit, seeds):
    """Window extended one session past the fixture series (Mon 06-17): a
    weekday gap AFTER the first stored rate is still surfaced — inception must
    never absorb a real hole. (Backfill surfaces weekday gaps in the report;
    only an EMPTY series fails the run, unchanged from v1.1 — the nightly
    daily.py path is where a weekday gap is a hard failure.)"""
    s = clean_audit
    _clean_window(s)
    s.execute(text("DELETE FROM market.fx_rates_daily WHERE rate_date='2024-06-17'"))
    report = _run(s, seeds, markets=(), start=date(2024, 6, 10), end=date(2024, 6, 17))
    fx = report.fx["USDAUD"]
    assert fx.rows == 5                           # 06-10..06-14; nothing for 06-17
    assert fx.first_rate == date(2024, 6, 10)     # min stored within this session
    assert fx.missing_weekdays == 1               # 06-17 — a real post-inception gap
    assert fx.empty is False


def test_deep_window_double_run_is_idempotent(clean_audit, seeds):
    """Re-running the deep window double-writes nothing: bars, corporate
    actions, gates and FX all upsert on natural keys — resumable by design."""
    s = clean_audit
    _clean_window(s)
    first = _run(s, seeds)
    counts_sql = ("SELECT (SELECT count(*) FROM market.price_bars_daily),"
                  "(SELECT count(*) FROM market.corporate_actions),"
                  "(SELECT count(*) FROM market.data_quality_gates),"
                  "(SELECT count(*) FROM market.fx_rates_daily)")
    before = tuple(s.execute(text(counts_sql)).one())
    second = _run(s, seeds)
    assert tuple(s.execute(text(counts_sql)).one()) == before
    # verdicts are stable too: same honest reds, same inceptions
    assert second.markets["US"].red == first.markets["US"].red
    assert second.markets["US"].inceptions == first.markets["US"].inceptions


def test_inception_map_earliest_stored_bar_per_symbol(clean_audit, seeds):
    """inception_map: min(bar_date) per active symbol; symbols with no stored
    bars are ABSENT (fail-closed expected in evaluate_gate); market filter
    scopes the map."""
    s = clean_audit
    _clean_window(s)
    _run(s, seeds)
    # TINCC: active instrument with zero stored bars -> absent from the map
    s.execute(text("INSERT INTO market.instruments (symbol, exchange, market, "
                   "instrument_type, name, currency) VALUES "
                   "('TINCC','NYSE','US','stock','Test Inception C','USD') "
                   "ON CONFLICT (symbol, exchange) DO NOTHING"))
    us = inception_map(s, "US")
    assert us["TINCA"] == A_INCEPTION
    assert us["TINCB"] == B_INCEPTION
    assert "TINCC" not in us
    au = inception_map(s, "AU")
    assert "TINCA" not in au and "TINCB" not in au
    # unscoped map spans markets and still contains the US symbols
    assert inception_map(s)["TINCA"] == A_INCEPTION
