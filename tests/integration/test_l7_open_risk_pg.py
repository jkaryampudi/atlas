"""ADR-0014 L7 open-risk redefinition (stop-based + core-aware) against an
isolated Postgres. Run ONLY against a dedicated throwaway DB, never dev 'atlas'
or shared 'atlas_test':

    export ATLAS_TEST_DATABASE_URL="postgresql+psycopg://atlas:atlas_local_only@localhost:5432/atlas_test_l7"

Covers, in priority order:
  * THE SAFETY PIN: a SATELLITE position (is_core=false) with a MISSING stop
    STILL fails closed on L7 (its full value counts as open risk) — a dropped
    satellite stop is a bug and must never silently read as zero. Pinned at the
    engine level (test_risk_engine) AND end-to-end here through _build_book.
  * a large CORE book (is_core=true, no stop) does NOT block a satellite
    proposal on L7 — the whole reason for the redefinition.
  * settlement sets trading.positions.is_core from the proposal origin: true for
    a core_allocation proposal, false for an agent proposal (the default).
  * snapshot's reported open_risk_pct mirrors the gate: core contributes zero.
  * migration 0023 up/down cycle restores the schema exactly.

Nothing commits: pg_session/clean_audit roll back. The migration test restores head.
"""
import os
import subprocess
from datetime import UTC, date, datetime, timedelta

import pytest
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text

from atlas.core.clock import FrozenClock
from atlas.dcp.risk.seed_limits import seed_limit_set
from atlas.dcp.trading.proposals import build_proposal, settle_orders, snapshot
from tests.conftest import URL, _ensure_test_db, requires_pg

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
T0 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)   # first day limit_set v1 is effective
NEXT_SESSION = date(2026, 7, 14)                 # XNYS session after 2026-07-13 (Mon)
FX_USD_AUD = Decimal("1.5")
_HIST = [date(2026, 6, 23) + timedelta(days=i) for i in range(21)]   # >=20 sessions


# ------------------------------------------------------------------- seeding

def _clean(s) -> None:
    s.execute(text("UPDATE trading.trade_proposals SET risk_check_id = NULL, state = 'draft'"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks", "trading.trade_proposals",
              "trading.positions", "trading.portfolio_snapshots"):
        s.execute(text(f"DELETE FROM {t}"))
    s.execute(text("DELETE FROM risk.limit_sets WHERE version > 1"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZL7%')"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol LIKE 'ZL7%'"))


def _etf(s, symbol: str) -> str:
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        " name, sector_gics, currency) "
        "VALUES (:sym, 'XTEST', 'US', 'etf', :sym, 'Broad', 'USD') RETURNING id"),
        {"sym": symbol}).scalar())


def _stock(s, symbol: str) -> str:
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        " name, sector_gics, currency) "
        "VALUES (:sym, 'XTEST', 'US', 'stock', :sym, 'Information Technology', 'USD') "
        "RETURNING id"), {"sym": symbol}).scalar())


def _bars(s, iid: str, close: Decimal, days=_HIST) -> None:
    s.execute(text(
        "INSERT INTO market.price_bars_daily "
        "(instrument_id, bar_date, open, close, volume, source) "
        "VALUES (:iid, :d, :o, :c, 1000000, 'EodhdAdapter')"),
        [{"iid": iid, "d": d, "o": close, "c": close} for d in days])


def _fx(s, days) -> None:
    for d in days:
        s.execute(text(
            "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
            "VALUES ('USD', 'AUD', :d, :r, 'test') "
            "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = :r"),
            {"d": d, "r": FX_USD_AUD})


def _memo(s) -> str:
    return str(s.execute(text(
        "INSERT INTO research.memos (memo_type, recommendation, evidence_refs) "
        "VALUES ('committee', 'BUY', '[]') RETURNING id")).scalar())


def _deploy(s, iid: str, qty: int, price: Decimal, *, is_core: bool,
            stop: Decimal | None) -> None:
    """A consistent filled long whose buy execution deducts the cash ledger so
    NAV = cash + holdings stays A$100k. is_core / stop are set on the position
    exactly as a real settlement would (core -> is_core true, no stop)."""
    rc = s.execute(text(
        "INSERT INTO risk.risk_checks (results, verdict, check_kind) "
        "VALUES ('[]', 'PASS', 'proposal') RETURNING id")).scalar()
    ap = s.execute(text(
        "INSERT INTO trading.approvals (decision, approver, approval_time_risk_check_id) "
        "VALUES ('approve', 'principal', :c) RETURNING id"), {"c": rc}).scalar()
    o = s.execute(text(
        "INSERT INTO trading.orders (approval_id, risk_check_id, side, qty, state) "
        "VALUES (:a, :c, 'buy', :q, 'filled') RETURNING id"),
        {"a": ap, "c": rc, "q": qty}).scalar()
    s.execute(text(
        "INSERT INTO trading.executions (order_id, fill_qty, fill_price, fees, fx_rate_used) "
        "VALUES (:o, :q, :p, 0, :fx)"), {"o": o, "q": qty, "p": price, "fx": FX_USD_AUD})
    s.execute(text(
        "INSERT INTO trading.positions (instrument_id, qty, avg_cost, currency, "
        " opened_at, current_stop, is_core) "
        "VALUES (:i, :q, :p, 'USD', :t, :s, :core)"),
        {"i": iid, "q": qty, "p": price, "t": datetime(2026, 7, 9, 15, 0, tzinfo=UTC),
         "s": stop, "core": is_core})


def _build_satellite(s, clock, memo_id: str, symbol: str):
    """A small satellite BUY through the real engine — L1 binds its size, so it
    is L7 (aggregate open risk) that discriminates the core vs satellite book."""
    return build_proposal(
        s, clock, memo_id=memo_id, symbol=symbol, signal_refs=[str(uuid4())],
        entry_price=Decimal("100"), stop_price=Decimal("95"),
        target_price=Decimal("120"))


# ------------------------------------------------- L7 through the book-builder

def test_large_core_book_does_not_block_satellite_on_l7(clean_audit):
    """A 70% CORE holding with NO stop contributes ZERO to aggregate open risk,
    so a satellite proposal is NOT blocked on L7 (ADR-0014)."""
    s = clean_audit
    _clean(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    core = _etf(s, "ZL7CORE")
    sat = _stock(s, "ZL7SAT")
    _bars(s, core, Decimal("100"))
    _bars(s, sat, Decimal("100"))
    _fx(s, _HIST)
    memo = _memo(s)
    # 460 * 100 * 1.5 = A$69,000 core exposure; the buy execution leaves
    # A$31,000 cash, NAV stays A$100,000. No stop -> core is stopless-legit.
    _deploy(s, core, 460, Decimal("100"), is_core=True, stop=None)

    res = _build_satellite(s, FrozenClock(T0), memo, "ZL7SAT")
    assert res.qty > 0                       # sizing succeeded, so L7 was evaluated
    assert "L7" not in res.failures          # the core added nothing to open risk


def test_satellite_missing_stop_still_blocks_on_l7(clean_audit):
    """THE SAFETY PIN, end-to-end: the SAME A$69,000 book, but the position is a
    SATELLITE (is_core=false) with a dropped stop. It now FAILS CLOSED — its full
    value counts as open risk and L7 blocks the satellite proposal. Only the
    is_core marker differs from the passing case above."""
    s = clean_audit
    _clean(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    core = _etf(s, "ZL7CORE")
    sat = _stock(s, "ZL7SAT")
    _bars(s, core, Decimal("100"))
    _bars(s, sat, Decimal("100"))
    _fx(s, _HIST)
    memo = _memo(s)
    _deploy(s, core, 460, Decimal("100"), is_core=False, stop=None)  # satellite, no stop

    res = _build_satellite(s, FrozenClock(T0), memo, "ZL7SAT")
    assert res.qty > 0                       # same sizing, so L7 was evaluated
    assert "L7" in res.failures              # fail closed: A$69k full value at risk


# --------------------------------------------------- settlement sets is_core

def _settle_buy(s, clock, *, iid: str, origin: str, stop: Decimal | None,
                qty: int, entry: Decimal, memo_id: str | None) -> dict:
    """Scaffold an APPROVED buy (agent or core) with genuine settle_orders
    lineage, seed the next session's open bar + its FX, and settle it via the
    REAL settle_orders -> _record_fill path. Returns the opened position row."""
    exp = T0 + timedelta(hours=24)
    if origin == "core_allocation":       # no memo, empty signals, NO stop (ADR-0012)
        pid = s.execute(text(
            "INSERT INTO trading.trade_proposals (instrument_id, market, action, origin, "
            " signal_ids, entry_price, target_price, position_size, state, expires_at, "
            " created_at) VALUES (:i,'US','buy','core_allocation','{}',:e,:e,:q,"
            " 'approved',:x,:c) RETURNING id"),
            {"i": iid, "e": entry, "q": qty, "x": exp, "c": T0}).scalar()
    else:                                  # agent: memo + signal + stop (invariant 2)
        pid = s.execute(text(
            "INSERT INTO trading.trade_proposals (instrument_id, market, action, "
            " committee_memo_id, signal_ids, entry_price, stop_loss, target_price, "
            " position_size, state, expires_at, created_at) "
            "VALUES (:i,'US','buy',:m,:sig,:e,:s,:e,:q,'approved',:x,:c) RETURNING id"),
            {"i": iid, "m": memo_id, "sig": [uuid4()], "e": entry, "s": stop,
             "q": qty, "x": exp, "c": T0}).scalar()
    rc = s.execute(text(
        "INSERT INTO risk.risk_checks (proposal_id, price_snapshot, results, verdict, "
        " check_kind) VALUES (:p, '{}', '[]', 'PASS', 'approval_time') RETURNING id"),
        {"p": pid}).scalar()
    ap = s.execute(text(
        "INSERT INTO trading.approvals (proposal_id, decision, approver, "
        " approval_time_risk_check_id, decided_at) "
        "VALUES (:p,'approve','principal',:c,:t) RETURNING id"),
        {"p": pid, "c": rc, "t": T0}).scalar()
    s.execute(text(
        "INSERT INTO trading.orders (proposal_id, approval_id, risk_check_id, broker, "
        " side, qty, order_type, state, created_at) "
        "VALUES (:p,:a,:c,'paper','buy',:q,'market','pending_submit',:t)"),
        {"p": pid, "a": ap, "c": rc, "q": qty, "t": T0})
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, close, "
        " volume, source) VALUES (:i,:d,:o,:o,1000000,'EodhdAdapter')"),
        {"i": iid, "d": NEXT_SESSION, "o": entry})
    _fx(s, [NEXT_SESSION])
    clock.advance_to(datetime(2026, 7, 14, 22, 0, tzinfo=UTC))
    fills = settle_orders(s, clock)
    assert len(fills) == 1
    return dict(s.execute(text(
        "SELECT is_core, current_stop, qty FROM trading.positions "
        "WHERE instrument_id = :i AND closed_at IS NULL"), {"i": iid}).one()._mapping)


def test_settlement_marks_core_allocation_position_is_core(clean_audit):
    s = clean_audit
    _clean(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    iid = _etf(s, "ZL7SPY")
    pos = _settle_buy(s, FrozenClock(T0), iid=iid, origin="core_allocation",
                      stop=None, qty=50, entry=Decimal("100"), memo_id=None)
    assert pos["is_core"] is True            # settlement flipped the marker true
    assert pos["current_stop"] is None       # core is rebalanced, not stopped


def test_settlement_marks_agent_position_not_core(clean_audit):
    s = clean_audit
    _clean(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    iid = _stock(s, "ZL7AGT")
    memo = _memo(s)
    pos = _settle_buy(s, FrozenClock(T0), iid=iid, origin="agent",
                      stop=Decimal("95"), qty=40, entry=Decimal("100"), memo_id=memo)
    assert pos["is_core"] is False           # the invariant-preserving default
    assert pos["current_stop"] == Decimal("95.000000")


def test_cross_origin_merge_is_refused(clean_audit):
    """Adversarial-review finding (2026-07-16): the one-open-row-per-instrument
    index forces a same-instrument add through the merge branch. An AGENT buy
    folding into an open CORE row must FAIL CLOSED — otherwise the agent's
    stopped shares inherit is_core=true and their L7 stop-out risk is zeroed."""
    s = clean_audit
    _clean(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    iid = _etf(s, "ZL7XMRG")
    memo = _memo(s)
    _deploy(s, iid, 50, Decimal("100"), is_core=True, stop=None)  # open CORE row
    # an approved AGENT buy for the SAME instrument, headed for settlement
    clock = FrozenClock(T0)
    pid = s.execute(text(
        "INSERT INTO trading.trade_proposals (instrument_id, market, action, "
        " committee_memo_id, signal_ids, entry_price, stop_loss, target_price, "
        " position_size, state, expires_at, created_at) "
        "VALUES (:i,'US','buy',:m,:sig,:e,:s,:e,:q,'approved',:x,:c) RETURNING id"),
        {"i": iid, "m": memo, "sig": [uuid4()], "e": Decimal("100"),
         "s": Decimal("95"), "q": 10, "x": T0 + timedelta(hours=24), "c": T0}).scalar()
    rc = s.execute(text(
        "INSERT INTO risk.risk_checks (proposal_id, price_snapshot, results, verdict, "
        " check_kind) VALUES (:p,'{}','[]','PASS','approval_time') RETURNING id"),
        {"p": pid}).scalar()
    ap = s.execute(text(
        "INSERT INTO trading.approvals (proposal_id, decision, approver, "
        " approval_time_risk_check_id, decided_at) "
        "VALUES (:p,'approve','principal',:c,:t) RETURNING id"),
        {"p": pid, "c": rc, "t": T0}).scalar()
    s.execute(text(
        "INSERT INTO trading.orders (proposal_id, approval_id, risk_check_id, broker, "
        " side, qty, order_type, state, created_at) "
        "VALUES (:p,:a,:c,'paper','buy',10,'market','pending_submit',:t)"),
        {"p": pid, "a": ap, "c": rc, "t": T0})
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, close, "
        " volume, source) VALUES (:i,:d,100,100,1000000,'EodhdAdapter')"),
        {"i": iid, "d": NEXT_SESSION})
    _fx(s, [NEXT_SESSION])
    clock.advance_to(datetime(2026, 7, 14, 22, 0, tzinfo=UTC))
    with pytest.raises(RuntimeError, match="cross-origin merge refused"):
        settle_orders(s, clock)


# ------------------------------------------------- snapshot open_risk_pct

def test_snapshot_open_risk_excludes_core(clean_audit):
    """The reported open_risk_pct must not disagree with the L7 gate: a core
    position contributes ZERO; only the stopped satellite's stop-out loss counts."""
    s = clean_audit
    _clean(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    core = _etf(s, "ZL7CORE")
    sat = _stock(s, "ZL7SAT")
    _bars(s, core, Decimal("100"))
    _bars(s, sat, Decimal("100"))
    _fx(s, _HIST)
    # core 200 * 100 * 1.5 = A$30k (no stop); satellite 100 * 100 * 1.5 = A$15k
    # with a 95 stop -> stop-out risk (100-95)*100*1.5 = A$750. cash A$55k, NAV A$100k.
    _deploy(s, core, 200, Decimal("100"), is_core=True, stop=None)
    _deploy(s, sat, 100, Decimal("100"), is_core=False, stop=Decimal("95"))

    snap = snapshot(s, FrozenClock(datetime(2026, 7, 13, 22, 30, tzinfo=UTC)))
    assert snap.nav_aud == Decimal("100000.00")
    # A$750 / A$100k = 0.0075. If the core's A$30k were (wrongly) counted, this
    # would be 0.3075 — the assertion pins the core-exclusion.
    assert snap.open_risk_pct == Decimal("0.0075")


# ------------------------------------------------- migration 0023 up/down

def _is_core_present() -> bool:
    eng = create_engine(URL)
    try:
        with eng.connect() as c:
            return c.execute(text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema='trading' AND table_name='positions' "
                "AND column_name='is_core'")).scalar() == 1
    finally:
        eng.dispose()


def test_migration_0023_down_up_restores_is_core():
    _ensure_test_db()

    def alembic(*args: str) -> None:
        r = subprocess.run(["alembic", *args], cwd=ROOT,
                           env={**os.environ, "ATLAS_DATABASE_URL": URL},
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    try:
        assert _is_core_present()                 # head has the column
        alembic("downgrade", "0022")
        assert not _is_core_present()             # downgrade drops it
        alembic("upgrade", "head")
        assert _is_core_present()                 # upgrade re-adds it
    finally:
        alembic("upgrade", "head")   # never leave the shared test DB downgraded
