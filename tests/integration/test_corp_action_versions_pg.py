"""F-007 part 2: knowledge-time versioning of corporate actions.

Corporate actions are the dominant mutable input to adjusted/total-return numbers
(bars are raw; adjustment is read-side). record_split/record_dividend now capture
an append-only version (append-on-change) with a knowledge_date, so a run pinned
at K reproduces the splits/dividends known then. The head table is unchanged, so
as-of=now equals the head."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.bar_versions import set_knowledge_date
from atlas.dcp.market_data.corp_action_versions import (
    corp_action_version_count, load_dividends_asof, load_splits_asof)
from atlas.dcp.market_data.ingest import record_dividend, record_split
from atlas.dcp.market_data.models import Dividend, Split
from tests.conftest import requires_pg

pytestmark = requires_pg


def _inst(s, symbol="ZZ") -> str:
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type,"
        " name, currency, is_active) VALUES (:s,'US','US','stock',:s,'USD',true)"
        " RETURNING id"), {"s": symbol}).scalar())


def test_split_correction_versions_and_asof_reproduces(pg_session):
    s = pg_session
    iid = _inst(s)
    set_knowledge_date(s, FrozenClock(datetime(2026, 1, 10, tzinfo=UTC)))
    record_split(s, iid, Split("ZZ", date(2025, 6, 2), Decimal(2)), "t")     # v1: 2:1
    set_knowledge_date(s, FrozenClock(datetime(2026, 2, 10, tzinfo=UTC)))
    record_split(s, iid, Split("ZZ", date(2025, 6, 2), Decimal(3)), "t")     # correction 3:1
    assert corp_action_version_count(s, iid, date(2025, 6, 2), "split") == 2
    before = load_splits_asof(s, iid, known_by=datetime(2026, 1, 20, tzinfo=UTC))
    after = load_splits_asof(s, iid, known_by=datetime(2026, 2, 20, tzinfo=UTC))
    assert [x.ratio for x in before] == [Decimal(2)]     # the ratio a run on 01-20 saw
    assert [x.ratio for x in after] == [Decimal(3)]       # the corrected ratio


def test_unchanged_reingest_adds_no_version(pg_session):
    s = pg_session
    iid = _inst(s)
    set_knowledge_date(s, FrozenClock(datetime(2026, 1, 10, tzinfo=UTC)))
    record_split(s, iid, Split("ZZ", date(2025, 6, 2), Decimal(2)), "t")
    record_split(s, iid, Split("ZZ", date(2025, 6, 2), Decimal(2)), "t")   # identical
    assert corp_action_version_count(s, iid, date(2025, 6, 2), "split") == 1


def test_dividend_versioning_and_asof(pg_session):
    s = pg_session
    iid = _inst(s)
    set_knowledge_date(s, FrozenClock(datetime(2026, 1, 10, tzinfo=UTC)))
    record_dividend(s, iid, Dividend("ZZ", date(2025, 3, 1), Decimal("0.22"), "USD"), "t")
    set_knowledge_date(s, FrozenClock(datetime(2026, 2, 10, tzinfo=UTC)))
    record_dividend(s, iid, Dividend("ZZ", date(2025, 3, 1), Decimal("0.25"), "USD"), "t")
    before = load_dividends_asof(s, iid, known_by=datetime(2026, 1, 20, tzinfo=UTC))
    after = load_dividends_asof(s, iid, known_by=datetime(2026, 2, 20, tzinfo=UTC))
    assert [d.amount for d in before] == [Decimal("0.22")]
    assert [d.amount for d in after] == [Decimal("0.25")]


def test_asof_now_equals_head_and_lookahead(pg_session):
    """as-of=now returns the head values; a run pinned before the action was
    known sees nothing (no look-ahead on knowledge_date)."""
    s = pg_session
    iid = _inst(s)
    set_knowledge_date(s, FrozenClock(datetime(2026, 3, 10, tzinfo=UTC)))
    record_split(s, iid, Split("ZZ", date(2025, 6, 2), Decimal(2)), "t")
    now_splits = load_splits_asof(s, iid, known_by=datetime(2026, 6, 1, tzinfo=UTC))
    head = s.execute(text("SELECT ratio FROM market.corporate_actions "
                          "WHERE instrument_id=:i AND action_type='split'"),
                     {"i": iid}).all()
    assert [x.ratio for x in now_splits] == [Decimal(r.ratio) for r in head]
    # pinned before the split was known -> invisible
    assert load_splits_asof(s, iid, known_by=datetime(2026, 1, 1, tzinfo=UTC)) == []


def test_through_caps_event_date(pg_session):
    s = pg_session
    iid = _inst(s)
    set_knowledge_date(s, FrozenClock(datetime(2026, 3, 10, tzinfo=UTC)))
    record_split(s, iid, Split("ZZ", date(2025, 6, 2), Decimal(2)), "t")
    record_split(s, iid, Split("ZZ", date(2025, 9, 2), Decimal(2)), "t")
    known = datetime(2026, 6, 1, tzinfo=UTC)
    assert len(load_splits_asof(s, iid, known_by=known)) == 2
    assert len(load_splits_asof(s, iid, known_by=known, through=date(2025, 7, 1))) == 1


def test_head_adopts_correction_so_asof_now_equals_head(pg_session):
    """Adversarial-review fix: a corrected split must reach the HEAD too (not just
    the version sidecar), or the live path stays on the stale value and
    known_by=now diverges from the head."""
    s = pg_session
    iid = _inst(s)
    set_knowledge_date(s, FrozenClock(datetime(2026, 1, 10, tzinfo=UTC)))
    record_split(s, iid, Split("ZZ", date(2025, 6, 2), Decimal(2)), "t")
    set_knowledge_date(s, FrozenClock(datetime(2026, 2, 10, tzinfo=UTC)))
    record_split(s, iid, Split("ZZ", date(2025, 6, 2), Decimal(3)), "t")   # correction
    head = s.execute(text("SELECT ratio FROM market.corporate_actions "
                          "WHERE instrument_id=:i"), {"i": iid}).scalar()
    assert Decimal(head) == Decimal(3)                 # head ADOPTED the correction
    asof_now = load_splits_asof(s, iid, known_by=datetime(2026, 6, 1, tzinfo=UTC))
    assert [x.ratio for x in asof_now] == [Decimal(3)]  # as-of-now == head, no divergence


def test_currency_only_dividend_correction_is_versioned(pg_session):
    """Adversarial-review fix: change-detection must include currency, or a
    currency-only correction is silently dropped."""
    s = pg_session
    iid = _inst(s)
    set_knowledge_date(s, FrozenClock(datetime(2026, 1, 10, tzinfo=UTC)))
    record_dividend(s, iid, Dividend("ZZ", date(2025, 3, 1), Decimal("0.5"), "USD"), "t")
    set_knowledge_date(s, FrozenClock(datetime(2026, 2, 10, tzinfo=UTC)))
    record_dividend(s, iid, Dividend("ZZ", date(2025, 3, 1), Decimal("0.5"), "INR"), "t")
    assert corp_action_version_count(s, iid, date(2025, 3, 1), "dividend") == 2
    after = load_dividends_asof(s, iid, known_by=datetime(2026, 6, 1, tzinfo=UTC))
    assert after[0].currency == "INR"


def test_adjusted_dividends_reproduce_asof_run_date(pg_session):
    """Adversarial-review fix: total_return.load_adjusted_dividends is now
    as-of-capable, so a TR run pinned at K reproduces its dividend inputs."""
    from atlas.dcp.market_data.total_return import load_adjusted_dividends
    s = pg_session
    iid = _inst(s)
    set_knowledge_date(s, FrozenClock(datetime(2026, 1, 10, tzinfo=UTC)))
    record_dividend(s, iid, Dividend("ZZ", date(2025, 3, 1), Decimal("0.20"), "USD"), "t")
    set_knowledge_date(s, FrozenClock(datetime(2026, 2, 10, tzinfo=UTC)))
    record_dividend(s, iid, Dividend("ZZ", date(2025, 3, 1), Decimal("0.25"), "USD"), "t")
    before = load_adjusted_dividends(s, "ZZ", known_by=datetime(2026, 1, 20, tzinfo=UTC))
    after = load_adjusted_dividends(s, "ZZ", known_by=datetime(2026, 2, 20, tzinfo=UTC))
    assert [d.amount for d in before] == [Decimal("0.20")]   # reproduces the run's inputs
    assert [d.amount for d in after] == [Decimal("0.25")]


def test_corp_action_version_is_immutable(pg_session):
    s = pg_session
    iid = _inst(s)
    set_knowledge_date(s, FrozenClock(datetime(2026, 1, 10, tzinfo=UTC)))
    record_split(s, iid, Split("ZZ", date(2025, 6, 2), Decimal(2)), "t")
    with pytest.raises(Exception, match="append-only"):
        with s.begin_nested():
            s.execute(text("UPDATE market.corporate_actions_versions SET ratio=99 "
                           "WHERE instrument_id=:i"), {"i": iid})
    with pytest.raises(Exception, match="append-only"):
        with s.begin_nested():
            s.execute(text("DELETE FROM market.corporate_actions_versions "
                           "WHERE instrument_id=:i"), {"i": iid})
    assert corp_action_version_count(s, iid, date(2025, 6, 2), "split") == 1
