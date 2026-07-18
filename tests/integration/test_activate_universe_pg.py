"""Universe activation / reconciliation (atlas/tools/activate_universe.py,
ADR-0016 decisions 1, 3, 4) against the isolated test DB.

Activation matrix: eligible members flip; vendor-delisted (AGN-class), stale
bars, missing sector, missing/ambiguous instrument all fail closed with
recorded reasons; already-active members are no-ops. The sanity band refuses
a wildly-off count BEFORE any write. Apply emits ONE audit event carrying the
full symbol list and every exclusion with reasons, and extends the manifest
(seeds/universe.json) deterministically sorted. Reconcile mode deactivates
former members (stocks only — an ADR sharing a former member's ticker is
untouched) and leaves open positions alone. Dry runs write nothing.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.calendars import previous_trading_day
from atlas.tools import activate_universe as au
from tests.conftest import requires_pg

pytestmark = requires_pg

CLOCK = FrozenClock(datetime(2026, 7, 18, 10, 0, tzinfo=UTC))
FETCHED = datetime(2026, 7, 18, 9, 0, tzinfo=UTC)
THRESHOLD = au.freshness_threshold(au.FRESH_REF, au.MAX_STALE_SESSIONS)

ETF_ENTRY = {"symbol": "ZETF", "exchange": "NYSEARCA", "market": "US",
             "instrument_type": "etf", "name": "Test ETF",
             "sector_gics": "Broad", "currency": "USD",
             "economic_exposure": ["US"]}
ZUAJ_ENTRY = {"symbol": "ZUAJ", "exchange": "US", "market": "US",
              "instrument_type": "stock", "name": "ZUAJ",
              "sector_gics": "Materials", "currency": "USD",
              "economic_exposure": ["US"]}


def _clean(s) -> None:
    s.execute(text("DELETE FROM validation.index_membership"))
    s.execute(text(
        "DELETE FROM trading.positions WHERE instrument_id IN "
        "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZUA%')"))
    s.execute(text(
        "DELETE FROM market.price_bars_daily WHERE instrument_id IN "
        "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZUA%')"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol LIKE 'ZUA%'"))


def _member(s, ticker: str, *, active_now: bool = True,
            delisted: bool = False) -> None:
    s.execute(text(
        "INSERT INTO validation.index_membership (index_code, ticker, name, "
        "start_date, end_date, is_active_now, is_delisted, fetched_at) "
        "VALUES ('GSPC.INDX', :t, :t, '2020-01-02', NULL, :a, :d, :f)"),
        {"t": ticker, "a": active_now, "d": delisted, "f": FETCHED})


def _instrument(s, symbol: str, *, sector: str = "", active: bool = False,
                exchange: str = "US", itype: str = "stock") -> str:
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, sector_gics, currency, economic_exposure, "
        "is_active) VALUES (:sym, :ex, 'US', :ty, :sym, :sec, 'USD', "
        "string_to_array('US', '|'), :act) RETURNING id"),
        {"sym": symbol, "ex": exchange, "ty": itype, "sec": sector,
         "act": active}).scalar())


def _bar(s, iid: str, day) -> None:
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid, :d, 100, 100, 100, 100, 1000, 'EodhdAdapter')"),
        {"iid": iid, "d": day})


def _active(s, symbol: str) -> bool:
    return bool(s.execute(text(
        "SELECT bool_or(is_active) FROM market.instruments WHERE symbol = :s"),
        {"s": symbol}).scalar())


def _events(s, event_type: str) -> int:
    return s.execute(text(
        "SELECT count(*) FROM audit.decision_events WHERE event_type = :t"),
        {"t": event_type}).scalar()


def _last_payload(s, event_type: str):
    return s.execute(text(
        "SELECT payload FROM audit.decision_events WHERE event_type = :t "
        "ORDER BY seq DESC LIMIT 1"), {"t": event_type}).scalar()


@pytest.fixture
def seeded(pg_session, tmp_path):
    s = pg_session
    _clean(s)
    # ZUAA eligible; ZUAB eligible at the exact freshness boundary;
    # ZUAC AGN-class corpse; ZUAD one session too stale; ZUAE no sector;
    # ZUAF no bars; ZUAG already active; ZUAH no instrument row;
    # ZUAI ambiguous (two US-market rows).
    iid = _instrument(s, "ZUAA", sector="Information Technology")
    _bar(s, iid, au.FRESH_REF)
    iid = _instrument(s, "ZUAB", sector="Energy")
    _bar(s, iid, THRESHOLD)
    iid = _instrument(s, "ZUAC", sector="Health Care")
    _bar(s, iid, "2020-05-08")
    iid = _instrument(s, "ZUAD", sector="Financials")
    _bar(s, iid, previous_trading_day("US", THRESHOLD))
    iid = _instrument(s, "ZUAE", sector="")
    _bar(s, iid, au.FRESH_REF)
    _instrument(s, "ZUAF", sector="Utilities")
    iid = _instrument(s, "ZUAG", sector="Industrials", active=True,
                      exchange="NYSE")
    _bar(s, iid, au.FRESH_REF)
    iid = _instrument(s, "ZUAI", sector="Materials")
    _bar(s, iid, au.FRESH_REF)
    _instrument(s, "ZUAI", sector="Materials", exchange="NYSE")
    for sym in ("ZUAA", "ZUAB", "ZUAD", "ZUAE", "ZUAF", "ZUAG", "ZUAH",
                "ZUAI"):
        _member(s, sym)
    _member(s, "ZUAC", delisted=True)
    seeds = tmp_path / "universe.json"
    seeds.write_text(json.dumps([ETF_ENTRY], indent=2) + "\n")
    return s, seeds


@pytest.fixture
def seeded_reconcile(seeded):
    s, seeds = seeded
    # ZUAJ former member, locally active, holds an open position -> deactivate
    # ZUAK former member's ticker on an ACTIVE ADR -> type guard, untouched
    # ZUAL active local stock with no membership row -> untouched
    iid = _instrument(s, "ZUAJ", sector="Materials", active=True)
    s.execute(text(
        "INSERT INTO trading.positions (instrument_id, qty, avg_cost, "
        "currency, opened_at, created_at) "
        "VALUES (:iid, 10, 100, 'USD', :t, :t)"), {"iid": iid, "t": FETCHED})
    _member(s, "ZUAJ", active_now=False)
    _instrument(s, "ZUAK", sector="Financials", active=True, itype="adr",
                exchange="NYSE")
    _member(s, "ZUAK", active_now=False)
    _instrument(s, "ZUAL", sector="Energy", active=True, exchange="NASDAQ")
    seeds.write_text(json.dumps([ETF_ENTRY, ZUAJ_ENTRY], indent=2) + "\n")
    return s, seeds


def test_activation_plan_matrix(seeded):
    s, _ = seeded
    plan = au.build_plan(s)
    assert plan.mode == "activate"
    assert plan.to_activate == ("ZUAA", "ZUAB")
    assert plan.to_deactivate == ()
    assert plan.already_active == ("ZUAG",)
    assert plan.threshold == THRESHOLD
    assert dict(plan.excluded) == {
        "ZUAC": ("vendor-delisted", "stale-bars"),
        "ZUAD": ("stale-bars",),
        "ZUAE": ("no-sector",),
        "ZUAF": ("no-bars",),
        "ZUAH": ("no-instrument",),
        "ZUAI": ("ambiguous-instrument",),
    }


def test_dry_run_writes_nothing(seeded):
    s, seeds = seeded
    before_seeds = seeds.read_bytes()
    before_events = s.execute(text(
        "SELECT count(*) FROM audit.decision_events")).scalar()
    au.build_plan(s)
    assert not _active(s, "ZUAA")
    assert seeds.read_bytes() == before_seeds
    assert s.execute(text(
        "SELECT count(*) FROM audit.decision_events")).scalar() == before_events


def test_sanity_band_refuses_before_any_write(seeded):
    s, seeds = seeded
    plan = au.build_plan(s)
    audit = PostgresAuditLog(s, CLOCK)
    before_seeds = seeds.read_bytes()
    with pytest.raises(ValueError, match="sanity band"):
        au.apply_plan(s, plan, audit=audit, seeds_path=seeds)   # default 350..420
    assert not _active(s, "ZUAA") and not _active(s, "ZUAB")
    assert _events(s, "market.universe.activated") == 0
    assert seeds.read_bytes() == before_seeds


def test_apply_flips_audits_once_and_extends_seeds(seeded):
    s, seeds = seeded
    plan = au.build_plan(s)
    audit = PostgresAuditLog(s, CLOCK)
    result = au.apply_plan(s, plan, audit=audit, seeds_path=seeds,
                           sanity_min=1, sanity_max=10)
    assert result.activated == ("ZUAA", "ZUAB")
    assert _active(s, "ZUAA") and _active(s, "ZUAB")
    for sym in ("ZUAC", "ZUAD", "ZUAE", "ZUAF", "ZUAI"):
        assert not _active(s, sym), sym
    assert _active(s, "ZUAG")                       # no-op, still active

    assert _events(s, "market.universe.activated") == 1
    payload = _last_payload(s, "market.universe.activated")
    assert payload["activated"] == ["ZUAA", "ZUAB"]
    assert payload["excluded"]["ZUAC"] == ["vendor-delisted", "stale-bars"]
    assert payload["excluded"]["ZUAE"] == ["no-sector"]
    assert payload["excluded"]["ZUAF"] == ["no-bars"]
    assert payload["already_active_count"] == 1
    assert payload["sanity_band"] == [1, 10]
    assert payload["freshness_threshold"] == THRESHOLD.isoformat()
    assert payload["seeds_added"] == ["ZUAA", "ZUAB"]

    entries = json.loads(seeds.read_text())
    assert [e["symbol"] for e in entries] == ["ZETF", "ZUAA", "ZUAB"]  # sorted
    zuaa = entries[1]
    assert zuaa == {"symbol": "ZUAA", "exchange": "US", "market": "US",
                    "instrument_type": "stock", "name": "ZUAA",
                    "sector_gics": "Information Technology",
                    "currency": "USD", "economic_exposure": ["US"]}
    # deterministic regeneration: a second manifest update is a byte no-op
    first = seeds.read_bytes()
    au.update_manifest(s, seeds, add=plan.to_activate, remove=())
    assert seeds.read_bytes() == first


def test_reconcile_deactivates_former_members_positions_untouched(
        seeded_reconcile):
    s, seeds = seeded_reconcile
    plan = au.build_plan(s, mode="reconcile")
    assert plan.mode == "reconcile"
    assert plan.to_activate == ("ZUAA", "ZUAB")     # same eligibility gates
    assert plan.to_deactivate == ("ZUAJ",)          # stock only: ZUAK is an ADR
    audit = PostgresAuditLog(s, CLOCK)
    result = au.apply_plan(s, plan, audit=audit, seeds_path=seeds)
    assert result.deactivated == ("ZUAJ",)
    assert not _active(s, "ZUAJ")
    assert _active(s, "ZUAK") and _active(s, "ZUAL")   # guards held

    # deactivation only stops new signals/ingest: the open position survives
    row = s.execute(text(
        "SELECT p.qty, p.closed_at FROM trading.positions p "
        "JOIN market.instruments i ON i.id = p.instrument_id "
        "WHERE i.symbol = 'ZUAJ'")).one()
    assert row.qty == 10 and row.closed_at is None

    assert _events(s, "market.universe.reconciled") == 1
    payload = _last_payload(s, "market.universe.reconciled")
    assert payload["activated"] == ["ZUAA", "ZUAB"]
    assert payload["deactivated"] == ["ZUAJ"]
    assert payload["seeds_removed"] == ["ZUAJ"]

    entries = json.loads(seeds.read_text())
    assert [e["symbol"] for e in entries] == ["ZETF", "ZUAA", "ZUAB"]


def test_reconcile_drift_cap_refuses_before_any_write(seeded_reconcile):
    s, seeds = seeded_reconcile
    plan = au.build_plan(s, mode="reconcile")
    audit = PostgresAuditLog(s, CLOCK)
    before_seeds = seeds.read_bytes()
    with pytest.raises(ValueError, match="drift cap"):
        au.apply_plan(s, plan, audit=audit, seeds_path=seeds, max_changes=1)
    assert not _active(s, "ZUAA")
    assert _active(s, "ZUAJ")
    assert _events(s, "market.universe.reconciled") == 0
    assert seeds.read_bytes() == before_seeds


def test_activate_mode_never_deactivates(seeded_reconcile):
    s, seeds = seeded_reconcile
    plan = au.build_plan(s)                          # mode="activate"
    assert plan.to_deactivate == ()
    audit = PostgresAuditLog(s, CLOCK)
    au.apply_plan(s, plan, audit=audit, seeds_path=seeds,
                  sanity_min=1, sanity_max=10)
    assert _active(s, "ZUAJ")                        # former member untouched


def test_update_manifest_refuses_missing_sector(seeded):
    s, seeds = seeded
    with pytest.raises(ValueError, match="sector"):
        au.update_manifest(s, seeds, add=("ZUAE",), remove=())


def test_freshness_boundary_is_exact(seeded):
    """A bar exactly AT the threshold is fresh; one session older is stale
    (ZUAB vs ZUAD in the matrix) — pinned here as its own statement."""
    s, _ = seeded
    plan = au.build_plan(s)
    assert "ZUAB" in plan.to_activate
    assert dict(plan.excluded)["ZUAD"] == ("stale-bars",)
