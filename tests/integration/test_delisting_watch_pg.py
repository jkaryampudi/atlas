"""Delisting watch (the SATS/EA pattern): detection is bounded and read-only;
the one-click deactivation re-verifies at the vendor, refuses held names, and
is audited. Every guard fails CLOSED with its reason."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.delisting import (
    DelistingError,
    deactivate_delisted,
    find_delisting_candidates,
)
from tests.conftest import requires_pg

pytestmark = requires_pg

CLOCK = FrozenClock(datetime(2026, 8, 7, 22, 0, tzinfo=UTC))  # US 08-07 completed


class _Vendor:
    """Configurable fundamentals stub; records which symbols were probed so the
    zero-candidates-zero-calls bound is provable."""
    def __init__(self, payloads):
        self.payloads = payloads
        self.probed: list[str] = []

    def fetch_fundamentals(self, symbol):
        self.probed.append(symbol)
        p = self.payloads.get(symbol)
        if isinstance(p, Exception):
            raise p
        return p


def _inst(s, sym, *, active=True, last_bar: date | None):
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type,"
        " name, currency, is_active) VALUES (:s,'US','US','stock',:s,'USD',:a) "
        "RETURNING id"), {"s": sym, "a": active}).scalar()
    if last_bar is not None:
        d = last_bar - timedelta(days=4)
        while d <= last_bar:
            if d.weekday() < 5:
                s.execute(text(
                    "INSERT INTO market.price_bars_daily (instrument_id, bar_date, "
                    " open, high, low, close, volume, source) "
                    "VALUES (:i,:d,100,100,100,100,1000,'EodhdAdapter')"),
                    {"i": iid, "d": d})
            d += timedelta(days=1)
    return iid


def test_detection_is_bounded_and_probes_only_stale_names(pg_session):
    """Relative assertions on THIS test's seeds — the shared atlas_test may
    carry committed instruments from other tests (deleting them all trips FK
    references), and any such stale stranger is simply another fail-soft probe.
    probe_cap is raised explicitly so committed strangers can never trip the
    universe-stale refusal and make this test flaky."""
    s = pg_session
    _inst(s, "ZCUR", last_bar=date(2026, 8, 7))            # current -> not probed
    _inst(s, "ZDEAD", last_bar=date(2026, 7, 1))           # stale -> candidate
    _inst(s, "ZOFF", active=False, last_bar=date(2026, 6, 1))  # inactive -> ignored
    v = _Vendor({"ZDEAD": {"General": {"IsDelisted": True,
                                       "DelistedDate": "2026-07-01"}}})
    scan = find_delisting_candidates(s, CLOCK, lambda sym, exch: v,
                                     probe_cap=10_000)
    assert scan.probed is True and scan.note is None
    assert scan.stale_total >= 1
    by = {c.symbol: c for c in scan.candidates}
    assert "ZDEAD" in by and "ZCUR" not in by and "ZOFF" not in by
    assert by["ZDEAD"].vendor_delisted is True
    assert by["ZDEAD"].delisted_date == "2026-07-01"
    assert by["ZDEAD"].held is False
    assert "ZDEAD" in v.probed                             # the stale seed probed
    assert "ZCUR" not in v.probed and "ZOFF" not in v.probed  # bounded


def test_universe_wide_staleness_refuses_to_probe(pg_session):
    """The missed-cycle case (2026-08-11, live): when the whole book's bars
    are behind, the watch must say 'ingest is behind' with ZERO vendor calls —
    not probe 500 names serially on every console refresh."""
    s = pg_session
    for i in range(11):                       # 11 stale actives > default cap 10
        _inst(s, f"ZST{i:02d}", last_bar=date(2026, 7, 1))
    v = _Vendor({})
    scan = find_delisting_candidates(s, CLOCK, lambda sym, exch: v)
    assert scan.probed is False
    assert scan.candidates == []
    assert scan.stale_total >= 11
    assert scan.note is not None and "ingest is behind" in scan.note
    assert v.probed == []                     # the bound that matters


def test_deactivate_happy_path_is_audited(clean_audit):
    s = clean_audit
    s.execute(text("DELETE FROM validation.index_membership WHERE ticker='ZDEAD'"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol='ZDEAD')"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol='ZDEAD'"))
    iid = _inst(s, "ZDEAD", last_bar=date(2026, 7, 1))
    s.execute(text(
        "INSERT INTO validation.index_membership (index_code, ticker, name, "
        " start_date, end_date, is_active_now, is_delisted, fetched_at) "
        "VALUES ('GSPC.INDX','ZDEAD','ZDEAD', DATE '2020-01-01', NULL, true, "
        " false, :f)"), {"f": CLOCK.now()})
    v = _Vendor({"ZDEAD": {"General": {"IsDelisted": True,
                                       "DelistedDate": "2026-07-01"}}})
    res = deactivate_delisted(s, CLOCK, lambda sym, exch: v, "ZDEAD")
    assert res.delisted_date == date(2026, 7, 1)
    assert res.membership_rows == 1
    assert s.execute(text("SELECT is_active FROM market.instruments WHERE id=:i"),
                     {"i": iid}).scalar() is False
    mem = s.execute(text(
        "SELECT is_delisted, is_active_now, end_date FROM validation.index_membership "
        "WHERE ticker='ZDEAD' AND index_code='GSPC.INDX'")).one()
    assert mem.is_delisted is True and mem.is_active_now is False
    assert mem.end_date == date(2026, 7, 1)
    ev = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type='market.universe.deactivated' ORDER BY seq DESC LIMIT 1"
    )).scalar()
    assert ev is not None and ev["deactivated"] == ["ZDEAD"]
    # cleanup (clean_audit commits)
    s.execute(text("DELETE FROM validation.index_membership WHERE ticker='ZDEAD'"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id=:i"),
              {"i": iid})
    s.execute(text("DELETE FROM market.instruments WHERE id=:i"), {"i": iid})
    s.commit()


def test_deactivate_refuses_held_name(pg_session):
    s = pg_session
    iid = _inst(s, "ZHELD", last_bar=date(2026, 7, 1))
    opened = datetime(2026, 6, 1, tzinfo=UTC)
    s.execute(text(
        "INSERT INTO trading.positions (instrument_id, qty, avg_cost, currency, "
        " opened_at, current_stop, created_at) "
        "VALUES (:i, 10, 100, 'USD', :o, 90, :o)"), {"i": iid, "o": opened})
    v = _Vendor({"ZHELD": {"General": {"IsDelisted": True,
                                       "DelistedDate": "2026-07-01"}}})
    with pytest.raises(DelistingError, match="HELD"):
        deactivate_delisted(s, CLOCK, lambda sym, exch: v, "ZHELD")
    assert s.execute(text("SELECT is_active FROM market.instruments WHERE id=:i"),
                     {"i": iid}).scalar() is True          # untouched


def test_deactivate_refuses_when_vendor_does_not_confirm(pg_session):
    s = pg_session
    iid = _inst(s, "ZGAP", last_bar=date(2026, 7, 1))
    for payload in ({"General": {"IsDelisted": False}},          # still listed
                    {"General": {"IsDelisted": True}},           # no date
                    RuntimeError("vendor down")):                # probe failure
        v = _Vendor({"ZGAP": payload})
        with pytest.raises(DelistingError, match="does not confirm"):
            deactivate_delisted(s, CLOCK, lambda sym, exch: v, "ZGAP")
    assert s.execute(text("SELECT is_active FROM market.instruments WHERE id=:i"),
                     {"i": iid}).scalar() is True


def test_deactivate_refuses_unknown_and_inactive(pg_session):
    s = pg_session
    v = _Vendor({})
    with pytest.raises(DelistingError, match="unknown"):
        deactivate_delisted(s, CLOCK, lambda sym, exch: v, "ZNOPE")
    _inst(s, "ZGONE", active=False, last_bar=None)
    with pytest.raises(DelistingError, match="already inactive"):
        deactivate_delisted(s, CLOCK, lambda sym, exch: v, "ZGONE")
