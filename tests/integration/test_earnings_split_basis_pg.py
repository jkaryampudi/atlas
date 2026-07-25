"""F-009: a stock split AFTER first ingest must keep the earnings series on ONE
consistent basis (the reviewer's acceptance bar).

market.earnings_surprises is append-only (ON CONFLICT DO NOTHING). A re-ingest
after a split freezes the old-basis rows and appends new-basis rows, so the STORE
is genuinely mixed-basis. The fix re-bases on READ via split_basis_asof, so the
series a consumer sees is single-basis. These tests exercise the real store +
read path and assert the store IS mixed (proving the test fails against
pre-remediation raw reads) while the read is reconciled."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from atlas.dcp.market_data.earnings_history import EarningsSurprise, store_surprises
from atlas.dcp.research.financials_panel import _earnings_history
from tests.conftest import requires_pg

pytestmark = requires_pg


def _instrument(s, symbol="AAA") -> str:
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type,"
        " name, currency, is_active) VALUES (:s,'US','US','stock',:s,'USD',true)"
        " RETURNING id"), {"s": symbol}).scalar())


def _sur(fpe, report, actual, estimate) -> EarningsSurprise:
    return EarningsSurprise(
        symbol="AAA", fiscal_period_end=date.fromisoformat(fpe),
        report_date=date.fromisoformat(report), eps_actual=Decimal(actual),
        eps_estimate=Decimal(estimate), surprise_pct=None, before_after_market=None)


def _split(s, iid, d, ratio):
    s.execute(text(
        "INSERT INTO market.corporate_actions (instrument_id, action_date, "
        "action_type, ratio, source) VALUES (:i,:d,'split',:r,'test')"),
        {"i": iid, "d": date.fromisoformat(d), "r": Decimal(ratio)})


def _seed_split_across_ingests(s) -> str:
    """First ingest (pre-split), a 2:1 split, then a re-ingest (post-split). The
    old quarters stay frozen on the pre-split basis; the new quarter lands on the
    post-split basis -> a genuinely mixed store."""
    iid = _instrument(s)
    T1 = datetime(2025, 8, 1, tzinfo=UTC)     # pre-split fetch
    store_surprises(s, iid, [
        _sur("2025-03-31", "2025-04-20", "4.00", "3.80"),
        _sur("2025-06-30", "2025-07-20", "4.20", "4.00")],
        fetched_at=T1, source="test")
    _split(s, iid, "2025-08-15", "2")         # 2:1 split
    T2 = datetime(2025, 11, 1, tzinfo=UTC)    # post-split re-fetch: vendor re-based
    store_surprises(s, iid, [
        _sur("2025-03-31", "2025-04-20", "2.00", "1.90"),   # ignored (DO NOTHING)
        _sur("2025-06-30", "2025-07-20", "2.10", "2.00"),   # ignored (DO NOTHING)
        _sur("2025-09-30", "2025-10-20", "2.10", "2.00")],  # NEW, post-split basis
        fetched_at=T2, source="test")
    return iid


def test_store_is_mixed_basis_but_read_is_reconciled(pg_session):
    s = pg_session
    iid = _seed_split_across_ingests(s)

    # (a) the STORE is genuinely mixed: old quarters frozen pre-split (4.x), new
    # quarter post-split (2.10); split_basis_asof records each row's fetch epoch.
    raw = s.execute(text(
        "SELECT fiscal_period_end, eps_actual, split_basis_asof "
        "FROM market.earnings_surprises WHERE instrument_id=:i "
        "ORDER BY fiscal_period_end"), {"i": iid}).all()
    assert [(r.eps_actual, r.split_basis_asof) for r in raw] == [
        (Decimal("4.00"), date(2025, 8, 1)),   # Q1 frozen pre-split
        (Decimal("4.20"), date(2025, 8, 1)),   # Q2 frozen pre-split
        (Decimal("2.10"), date(2025, 11, 1))]  # Q3 post-split
    # ^ pre-remediation, a raw read returns exactly this mixed series (4.00, 4.20,
    #   2.10) — inconsistent. The fix reconciles it below.

    # (b) the READ (financials panel, real consumer) re-bases to the as_of basis:
    # every quarter lands on the post-split basis -> ONE consistent series.
    panel = _earnings_history(s, iid, as_of=date(2025, 12, 1))
    by_fpe = {row["fiscal_period_end"]: row["eps_actual"] for row in panel}
    assert by_fpe["2025-03-31"] == pytest.approx(2.00)   # 4.00 / 2  (re-based)
    assert by_fpe["2025-06-30"] == pytest.approx(2.10)   # 4.20 / 2  (re-based)
    assert by_fpe["2025-09-30"] == pytest.approx(2.10)   # already post-split
    # the mixed 4.x values are gone from the consumer's view — single basis.


def test_lookahead_split_only_affects_reads_on_or_after_its_date(pg_session):
    s = pg_session
    iid = _seed_split_across_ingests(s)

    # read just BEFORE the split date: Q1 is visible (reported 2025-04-20) and the
    # 2025-08-15 split is NOT yet knowable -> unchanged (4.00).
    before = {r["fiscal_period_end"]: r["eps_actual"]
              for r in _earnings_history(s, iid, as_of=date(2025, 8, 14))}
    assert before["2025-03-31"] == pytest.approx(4.00)   # split excluded (look-ahead safe)

    # read ON the split date (inclusive upper bound): Q1 re-bases (4.00 / 2).
    on = {r["fiscal_period_end"]: r["eps_actual"]
          for r in _earnings_history(s, iid, as_of=date(2025, 8, 15))}
    assert on["2025-03-31"] == pytest.approx(2.00)


def test_store_sets_split_basis_asof_to_fetch_date(pg_session):
    s = pg_session
    iid = _instrument(s, "BBB")
    store_surprises(s, iid, [_sur("2025-03-31", "2025-04-20", "1.0", "0.9")],
                    fetched_at=datetime(2025, 9, 10, 14, 30, tzinfo=UTC),
                    source="test")
    got = s.execute(text(
        "SELECT split_basis_asof FROM market.earnings_surprises "
        "WHERE instrument_id=:i"), {"i": iid}).scalar()
    assert got == date(2025, 9, 10)      # = fetched_at UTC date
