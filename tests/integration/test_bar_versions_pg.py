"""F-007: bitemporal (knowledge-time) bar versioning + as-of reads.

The head table market.price_bars_daily stays the fast 'latest'; every write also
appends to the append-only market.price_bars_versions via a trigger. A registered
run pins a knowledge instant K and reproduces the bars as they were KNOWN then,
even after a later correction. With K=now the as-of read equals the head, so
existing readers are unchanged."""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.bar_versions import (bar_version_count, load_bars_asof,
                                                revised_bars, set_knowledge_date)
from tests.conftest import requires_pg

pytestmark = requires_pg


def _inst(s, symbol="ZZ") -> str:
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type,"
        " name, currency, is_active) VALUES (:s,'US','US','stock',:s,'USD',true)"
        " RETURNING id"), {"s": symbol}).scalar())


def _write_bar(s, iid, d, *, o, h, lo, c, v=100, source="t") -> None:
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high,"
        " low, close, volume, source) VALUES (:i,:d,:o,:h,:l,:c,:v,:src) "
        "ON CONFLICT (instrument_id, bar_date) DO UPDATE SET "
        "  open=:o, high=:h, low=:l, close=:c, volume=:v, source=:src"),
        {"i": iid, "d": d, "o": o, "h": h, "l": lo, "c": c, "v": v, "src": source})


def test_asof_now_equals_head(pg_session):
    """K=now reproduces the head exactly — the byte-identity guarantee."""
    s = pg_session
    iid = _inst(s)
    set_knowledge_date(s, FrozenClock(datetime(2026, 1, 10, tzinfo=UTC)))
    for i, c in enumerate([10, 11, 12], start=1):
        _write_bar(s, iid, date(2026, 1, i), o=c, h=c + 5, lo=c - 1, c=c)
    asof = load_bars_asof(s, iid, through=date(2026, 1, 31),
                          known_by=datetime(2026, 6, 1, tzinfo=UTC))
    head = s.execute(text("SELECT bar_date, close FROM market.price_bars_daily "
                          "WHERE instrument_id=:i ORDER BY bar_date"), {"i": iid}).all()
    assert [(b.bar_date, b.close) for b in asof] == [(h.bar_date, h.close) for h in head]


def test_correction_appends_version_and_asof_reproduces(pg_session):
    """A corrected bar lands as a NEW version; a run pinned before the correction
    still reads the original value (reproducibility), one pinned after reads the
    corrected value."""
    s = pg_session
    iid = _inst(s)
    set_knowledge_date(s, FrozenClock(datetime(2026, 1, 10, tzinfo=UTC)))
    _write_bar(s, iid, date(2026, 1, 5), o=10, h=20, lo=9, c=10)      # v1 known 01-10
    set_knowledge_date(s, FrozenClock(datetime(2026, 2, 10, tzinfo=UTC)))
    _write_bar(s, iid, date(2026, 1, 5), o=10, h=20, lo=9, c=12)      # v2 known 02-10
    assert bar_version_count(s, iid, date(2026, 1, 5)) == 2
    assert revised_bars(s, iid) == [date(2026, 1, 5)]
    before = load_bars_asof(s, iid, through=date(2026, 1, 5),
                            known_by=datetime(2026, 1, 20, tzinfo=UTC))
    after = load_bars_asof(s, iid, through=date(2026, 1, 5),
                           known_by=datetime(2026, 2, 20, tzinfo=UTC))
    assert before[0].close == 10        # the value a run on 01-20 actually saw
    assert after[0].close == 12         # the corrected value


def test_no_op_reingest_appends_no_version(pg_session):
    s = pg_session
    iid = _inst(s)
    set_knowledge_date(s, FrozenClock(datetime(2026, 1, 10, tzinfo=UTC)))
    _write_bar(s, iid, date(2026, 1, 5), o=10, h=20, lo=9, c=10)
    _write_bar(s, iid, date(2026, 1, 5), o=10, h=20, lo=9, c=10)      # identical
    assert bar_version_count(s, iid, date(2026, 1, 5)) == 1          # no bloat


def test_no_lookahead_on_knowledge_date(pg_session):
    """An as-of read never surfaces a version learned after K."""
    s = pg_session
    iid = _inst(s)
    set_knowledge_date(s, FrozenClock(datetime(2026, 3, 10, tzinfo=UTC)))
    _write_bar(s, iid, date(2026, 1, 5), o=10, h=20, lo=9, c=10)
    # a run pinned BEFORE the bar was ever known sees nothing
    assert load_bars_asof(s, iid, through=date(2026, 1, 5),
                          known_by=datetime(2026, 1, 1, tzinfo=UTC)) == []
    # and on/after it does
    assert len(load_bars_asof(s, iid, through=date(2026, 1, 5),
                              known_by=datetime(2026, 3, 10, tzinfo=UTC))) == 1


def test_knowledge_date_comes_from_injected_clock(pg_session):
    """set_knowledge_date stamps the version from the injected clock, not now()."""
    s = pg_session
    iid = _inst(s)
    set_knowledge_date(s, FrozenClock(datetime(2025, 7, 1, 12, 0, tzinfo=UTC)))
    _write_bar(s, iid, date(2025, 6, 30), o=1, h=2, lo=1, c=1)
    kd = s.execute(text("SELECT knowledge_date FROM market.price_bars_versions "
                        "WHERE instrument_id=:i"), {"i": iid}).scalar()
    assert kd == datetime(2025, 7, 1, 12, 0, tzinfo=UTC)


def test_version_rows_are_immutable(pg_session):
    """The register's PG-immutability bar (probe P7): a direct overwrite OR delete
    of a stored version fails closed. Savepoints isolate each aborting statement
    so the shared instrument/bar survive."""
    s = pg_session
    iid = _inst(s)
    set_knowledge_date(s, FrozenClock(datetime(2026, 1, 10, tzinfo=UTC)))
    _write_bar(s, iid, date(2026, 1, 5), o=100, h=100, lo=100, c=100)

    with pytest.raises(Exception, match="append-only"):
        with s.begin_nested():                       # the 100 -> 250 overwrite
            s.execute(text("UPDATE market.price_bars_versions SET close=250 "
                           "WHERE instrument_id=:i"), {"i": iid})
    with pytest.raises(Exception, match="append-only"):
        with s.begin_nested():                       # history cannot be deleted
            s.execute(text("DELETE FROM market.price_bars_versions "
                           "WHERE instrument_id=:i"), {"i": iid})
    # the version is intact and unchanged
    assert s.execute(text("SELECT close FROM market.price_bars_versions "
                          "WHERE instrument_id=:i"), {"i": iid}).scalar() == 100
