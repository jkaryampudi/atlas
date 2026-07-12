"""Memo->proposal bridge (atlas/dcp/trading/bridge.py) against atlas_test —
ADR-0006 implemented to the letter.

Pins the deterministic price derivation end-to-end (entry from the latest
vendor close, Wilder ATR(14) over the last 15 complete OHLC sessions, the
2xATR stop vs the -10% floor BOTH ways, the 2R target, 6dp quanta), the
candidate filter (committee BUY only, non-shadow, fresh), every ADR-0006
scope guard as a recorded skip, per-memo idempotency in ANY state, the
fail-closed ATR window, and the trading.bridge.completed audit event's
ref->uuid lineage mapping. Seeding mirrors test_trading_lifecycle_pg.py.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.risk.seed_limits import seed_limit_set
from atlas.dcp.trading.bridge import bridge_memos, evidence_signal_id
from tests.conftest import requires_pg

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
# Monday 2026-07-13: the first day limit set v1 is effective (seeds/limit_set_v1.json)
T0 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
FX_USD_AUD = Decimal("1.5")
REFS = ("bars:ZBRA:2026-07-13", "gate:momentum_v1:ZBRA")


# ------------------------------------------------------------------- seeding

def _clean(s) -> None:
    """Remove any committed debris from crashed runs (FK-safe order)."""
    s.execute(text("UPDATE trading.trade_proposals "
                   "SET risk_check_id = NULL, state = 'draft'"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks", "trading.trade_proposals",
              "trading.positions", "trading.portfolio_snapshots"):
        s.execute(text(f"DELETE FROM {t}"))
    s.execute(text("DELETE FROM risk.limit_sets WHERE version > 1"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZBR%')"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol LIKE 'ZBR%'"))


def _seed(s) -> None:
    _clean(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD', 'AUD', '2026-07-10', :r, 'test') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = :r"),
        {"r": FX_USD_AUD})


def _instrument(s, symbol: str, *, sector: str = "Information Technology"):
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        "name, sector_gics, currency) "
        "VALUES (:sym, 'XTEST', 'US', 'stock', :sym, :sec, 'USD') RETURNING id"),
        {"sym": symbol, "sec": sector}).scalar()


def _ohlc(s, iid, *, days: int = 21, o=100, h=101, lo=99, c=100,
          volume: int = 1_000_000, start: date = date(2026, 6, 23)) -> None:
    """Full OHLC sessions ending 2026-07-13 for the default 21 days."""
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high, "
        "low, close, volume, source) "
        "VALUES (:iid, :d, :o, :h, :l, :c, :v, 'EodhdAdapter')"),
        [{"iid": iid, "d": start + timedelta(days=i), "o": o, "h": h, "l": lo,
          "c": c, "v": volume} for i in range(days)])


def _memo(s, clock, symbol: str | None, *, recommendation: str = "BUY",
          refs: tuple[str, ...] = REFS, memo_type: str = "committee",
          shadow: bool | None = None, created_at: datetime | None = None) -> str:
    """A research memo created at the injected clock's instant (the bridge's
    48h freshness window must never depend on the DB wall clock)."""
    agent_run_id = None
    if shadow is not None:
        agent_run_id = s.execute(text(
            "INSERT INTO research.agent_runs (agent_role, prompt_template_hash, "
            "model, status, shadow) "
            "VALUES ('cio', 'test-hash', 'test-model', 'ok', :sh) RETURNING id"),
            {"sh": shadow}).scalar()
    return str(s.execute(text(
        "INSERT INTO research.memos (agent_run_id, memo_type, instrument_symbol, "
        "recommendation, evidence_refs, created_at) "
        "VALUES (:ar, :mt, :sym, :rec, CAST(:er AS jsonb), :ca) RETURNING id"),
        {"ar": agent_run_id, "mt": memo_type, "sym": symbol,
         "rec": recommendation, "er": json.dumps(list(refs)),
         "ca": created_at if created_at is not None else clock.now()}).scalar())


def _proposals(s) -> list:
    return s.execute(text(
        "SELECT tp.*, i.symbol FROM trading.trade_proposals tp "
        "JOIN market.instruments i ON i.id = tp.instrument_id "
        "ORDER BY tp.created_at")).all()


def _bridge_event(s) -> dict:
    return s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'trading.bridge.completed' "
        "ORDER BY seq DESC LIMIT 1")).scalar_one()


# --------------------------------------------------- ADR-0006 price derivation

def test_calm_buy_memo_bridges_with_atr_stop(clean_audit):
    """Calm series: 21 sessions of h=101 l=99 c=100 -> every TR = 2 -> Wilder
    ATR(14) over the last 15 sessions = 2.0 exactly.
      entry  = latest close                      = 100.000000
      2xATR = 4 < 10% of entry = 10  ->  ATR stop binds (ADR-0006):
      stop   = 100 - 2*2                         =  96.000000
      target = 100 + 2*(100-96)                  = 108.000000
      qty (risk engine, empty A$100k book):
        L6 budget 1% * 100000 = A$1000 / ((100-96)*1.5) = 166.67
        L1 weight 8% * 100000 / (100*1.5)               =  53.33  <- binds
        L10 5% of ADV 1,000,000                         = 50,000
        -> floor(53.33) = 53 shares
    """
    s = clean_audit
    _seed(s)
    _ohlc(s, _instrument(s, "ZBRA"))
    clock = FrozenClock(T0)
    memo_id = _memo(s, clock, "ZBRA")

    report = bridge_memos(s, clock)
    assert report.skipped == ()
    assert len(report.built) == 1
    b = report.built[0]
    assert (b.symbol, b.memo_id, b.verdict, b.qty) == ("ZBRA", memo_id, "PASS", 53)
    assert report.summary() == "bridged 1 (ZBRA:PASS) · skipped 0"

    row = _proposals(s)[0]
    assert str(row.id) == b.proposal_id
    assert (row.state, row.action) == ("pending_approval", "buy")
    assert row.entry_price == Decimal("100.000000")
    assert row.stop_loss == Decimal("96.000000")
    assert row.target_price == Decimal("108.000000")
    assert row.position_size == 53
    assert str(row.committee_memo_id) == memo_id
    # signal_ids: deterministic uuid5 of the memo's evidence refs (ADR-0006
    # interim measure), in evidence order
    assert [str(u) for u in row.signal_ids] == [
        str(evidence_signal_id(r)) for r in REFS]
    # the lifecycle did the rest: a PASS proposal-time check is referenced
    check = s.execute(text(
        "SELECT verdict, check_kind FROM risk.risk_checks WHERE id = :c"),
        {"c": row.risk_check_id}).one()
    assert (check.verdict, check.check_kind) == ("PASS", "proposal")


def test_violent_buy_memo_clamps_stop_to_floor(clean_audit):
    """Violent series: h=104 l=97 c=100 -> every TR = 7 -> ATR(14) = 7.0.
      2xATR = 14 > 10% of entry = 10  ->  the -10% floor clamps (ADR-0006):
      stop   = max(100-14, 100*0.90) =  90.000000
      target = 100 + 2*(100-90)      = 120.000000
      qty: L6 1000/((100-90)*1.5) = 66.67; L1 53.33 still binds -> 53.
    """
    s = clean_audit
    _seed(s)
    _ohlc(s, _instrument(s, "ZBRV"), h=104, lo=97)
    clock = FrozenClock(T0)
    _memo(s, clock, "ZBRV")

    report = bridge_memos(s, clock)
    assert len(report.built) == 1 and report.built[0].verdict == "PASS"
    row = _proposals(s)[0]
    assert row.entry_price == Decimal("100.000000")
    assert row.stop_loss == Decimal("90.000000")
    assert row.target_price == Decimal("120.000000")
    assert row.position_size == 53


def test_risk_fail_is_an_honest_built_outcome(clean_audit):
    """A bridged proposal the risk engine rejects lands 'rejected' and is
    REPORTED in built with verdict FAIL — the gate working is a deliverable,
    never a bridge error (CLAUDE.md working style). Seeding mirrors the
    lifecycle L8 case: a thin-history holding fails correlation closed to 1
    and the combined weight breaches the 12% cap."""
    s = clean_audit
    _seed(s)
    _ohlc(s, _instrument(s, "ZBRA"))
    zid = _instrument(s, "ZBRB", sector="Financials")
    _ohlc(s, zid, days=5, start=date(2026, 7, 6))
    s.execute(text(
        "INSERT INTO trading.positions (instrument_id, qty, avg_cost, currency, "
        "opened_at, current_stop) VALUES (:iid, 70, 100, 'USD', :t, 90)"),
        {"iid": zid, "t": datetime(2026, 7, 10, 15, 0, tzinfo=UTC)})
    clock = FrozenClock(T0)
    _memo(s, clock, "ZBRA")

    report = bridge_memos(s, clock)
    assert report.skipped == ()
    assert len(report.built) == 1
    assert report.built[0].verdict == "FAIL"
    assert report.summary() == "bridged 1 (ZBRA:FAIL) · skipped 0"
    row = _proposals(s)[0]
    assert (row.symbol, row.state) == ("ZBRA", "rejected")
    fails = s.execute(text(
        "SELECT results FROM risk.risk_checks WHERE proposal_id = :p"),
        {"p": row.id}).scalar_one()
    assert any(r["rule"] == "L8" and not r["pass"] for r in fails)


# ------------------------------------------------------- candidacy and guards

def test_same_memo_never_bridges_twice(clean_audit):
    s = clean_audit
    _seed(s)
    _ohlc(s, _instrument(s, "ZBRA"))
    clock = FrozenClock(T0)
    memo_id = _memo(s, clock, "ZBRA")
    assert len(bridge_memos(s, clock).built) == 1

    clock.advance_to(T0 + timedelta(hours=1))
    again = bridge_memos(s, clock)          # idempotent re-run
    assert again.built == ()
    assert len(again.skipped) == 1
    assert again.skipped[0].memo_id == memo_id
    assert "already bridged" in again.skipped[0].reason

    # ANY state: even after the proposal leaves the live set, the memo stays
    # consumed — a fresh thesis needs a fresh memo, never a re-bridge
    s.execute(text("UPDATE trading.trade_proposals SET state = 'expired'"))
    third = bridge_memos(s, clock)
    assert third.built == ()
    assert "already bridged" in third.skipped[0].reason
    assert s.execute(text(
        "SELECT count(*) FROM trading.trade_proposals")).scalar() == 1


def test_reject_memo_never_bridges(clean_audit):
    s = clean_audit
    _seed(s)
    _ohlc(s, _instrument(s, "ZBRA"))
    clock = FrozenClock(T0)
    _memo(s, clock, "ZBRA", recommendation="REJECT")

    report = bridge_memos(s, clock)
    assert report.built == () and report.skipped == ()   # not a candidate
    assert _proposals(s) == []


def test_shadow_buy_memo_never_bridges(clean_audit):
    """ADR-0005 pattern 4: shadow output is non-actionable. A shadow BUY is
    not a candidate; the same memo from a NON-shadow run bridges."""
    s = clean_audit
    _seed(s)
    _ohlc(s, _instrument(s, "ZBRA"))
    clock = FrozenClock(T0)
    _memo(s, clock, "ZBRA", shadow=True)
    report = bridge_memos(s, clock)
    assert report.built == () and report.skipped == ()
    assert _proposals(s) == []

    _memo(s, clock, "ZBRA", shadow=False)    # explicit non-shadow run bridges
    report = bridge_memos(s, clock)
    assert len(report.built) == 1 and report.built[0].symbol == "ZBRA"


def test_stale_memo_falls_out_of_candidacy(clean_audit):
    """A BUY memo older than 48h is a stale thesis (module docstring): not a
    candidate at all, so neither built nor recorded as a skip."""
    s = clean_audit
    _seed(s)
    _ohlc(s, _instrument(s, "ZBRA"))
    clock = FrozenClock(T0)
    _memo(s, clock, "ZBRA", created_at=T0 - timedelta(hours=49))

    report = bridge_memos(s, clock)
    assert report.built == () and report.skipped == ()
    assert _proposals(s) == []


def test_open_position_symbol_skipped(clean_audit):
    s = clean_audit
    _seed(s)
    iid = _instrument(s, "ZBRA")
    _ohlc(s, iid)
    s.execute(text(
        "INSERT INTO trading.positions (instrument_id, qty, avg_cost, currency, "
        "opened_at, current_stop) VALUES (:iid, 10, 100, 'USD', :t, 95)"),
        {"iid": iid, "t": datetime(2026, 7, 10, 15, 0, tzinfo=UTC)})
    clock = FrozenClock(T0)
    _memo(s, clock, "ZBRA")

    report = bridge_memos(s, clock)
    assert report.built == ()
    assert len(report.skipped) == 1
    assert "open position" in report.skipped[0].reason
    assert _proposals(s) == []


def test_live_proposal_blocks_second_memo_same_symbol(clean_audit):
    """Two fresh BUY memos for one symbol in one run: the first bridges, the
    second hits the one-live-proposal-per-symbol guard (ADR-0006)."""
    s = clean_audit
    _seed(s)
    _ohlc(s, _instrument(s, "ZBRA"))
    clock = FrozenClock(T0)
    first = _memo(s, clock, "ZBRA", created_at=T0 - timedelta(minutes=2))
    second = _memo(s, clock, "ZBRA", created_at=T0 - timedelta(minutes=1))

    report = bridge_memos(s, clock)
    assert len(report.built) == 1 and report.built[0].memo_id == first
    assert len(report.skipped) == 1
    assert report.skipped[0].memo_id == second
    assert "live proposal" in report.skipped[0].reason
    assert len(_proposals(s)) == 1


def test_live_order_skipped(clean_audit):
    """A live order blocks even when its proposal has left the live-proposal
    states (defense in depth: the order IS standing buy intent)."""
    s = clean_audit
    _seed(s)
    iid = _instrument(s, "ZBRA")
    _ohlc(s, iid)
    clock = FrozenClock(T0)
    old_memo = _memo(s, clock, "ZBRA", created_at=T0 - timedelta(hours=72))
    pid = s.execute(text(
        "INSERT INTO trading.trade_proposals (instrument_id, market, action, "
        "committee_memo_id, signal_ids, entry_price, stop_loss, target_price, "
        "position_size, state, expires_at, created_at) "
        "VALUES (:iid, 'US', 'buy', :m, :sids, 100, 96, 108, 10, 'voided', "
        ":exp, :ca) RETURNING id"),
        {"iid": iid, "m": old_memo, "sids": [uuid.uuid4()],
         "exp": T0 - timedelta(hours=47), "ca": T0 - timedelta(hours=71)}).scalar()
    cid = s.execute(text(
        "INSERT INTO risk.risk_checks (proposal_id, results, verdict, check_kind) "
        "VALUES (:p, '[]', 'PASS', 'approval_time') RETURNING id"), {"p": pid}).scalar()
    aid = s.execute(text(
        "INSERT INTO trading.approvals (proposal_id, decision, approver, "
        "approval_time_risk_check_id) "
        "VALUES (:p, 'approve', 'principal', :c) RETURNING id"),
        {"p": pid, "c": cid}).scalar()
    s.execute(text(
        "INSERT INTO trading.orders (proposal_id, approval_id, risk_check_id, "
        "broker, side, qty, state) "
        "VALUES (:p, :a, :c, 'paper', 'buy', 10, 'pending_submit')"),
        {"p": pid, "a": aid, "c": cid})
    _memo(s, clock, "ZBRA")

    report = bridge_memos(s, clock)
    assert report.built == ()
    assert len(report.skipped) == 1
    assert "live order" in report.skipped[0].reason


# --------------------------------------------------------- fail-closed pricing

def test_null_ohlc_in_window_fails_closed(clean_audit):
    """A NULL high inside the 15-session ATR window means the volatility is
    unknowable -> no proposal (ADR-0006 fail closed), recorded as a skip."""
    s = clean_audit
    _seed(s)
    iid = _instrument(s, "ZBRA")
    _ohlc(s, iid)
    s.execute(text(
        "UPDATE market.price_bars_daily SET high = NULL "
        "WHERE instrument_id = :iid AND bar_date = '2026-07-10'"), {"iid": iid})
    clock = FrozenClock(T0)
    _memo(s, clock, "ZBRA")

    report = bridge_memos(s, clock)
    assert report.built == ()
    assert len(report.skipped) == 1
    assert "incomplete OHLC" in report.skipped[0].reason
    assert _proposals(s) == []


def test_fewer_than_15_sessions_fails_closed(clean_audit):
    s = clean_audit
    _seed(s)
    _ohlc(s, _instrument(s, "ZBRA"), days=10, start=date(2026, 7, 4))
    clock = FrozenClock(T0)
    _memo(s, clock, "ZBRA")

    report = bridge_memos(s, clock)
    assert report.built == ()
    assert "only 10 vendor sessions" in report.skipped[0].reason
    assert _proposals(s) == []


def test_empty_evidence_refs_skipped(clean_audit):
    """No evidence, no trade (Principle 1): signal_ids derive from the refs
    and must be non-empty, so a refless memo is skipped, never bridged."""
    s = clean_audit
    _seed(s)
    _ohlc(s, _instrument(s, "ZBRA"))
    clock = FrozenClock(T0)
    _memo(s, clock, "ZBRA", refs=())

    report = bridge_memos(s, clock)
    assert report.built == ()
    assert len(report.skipped) == 1
    assert "no evidence refs" in report.skipped[0].reason
    assert _proposals(s) == []


def test_unknown_symbol_skipped(clean_audit):
    s = clean_audit
    _seed(s)
    clock = FrozenClock(T0)
    _memo(s, clock, "ZBRX")                  # no such active instrument

    report = bridge_memos(s, clock)
    assert report.built == ()
    assert "exactly one active instrument" in report.skipped[0].reason


# ------------------------------------------------------------------ audit trail

def test_bridge_event_carries_ref_to_uuid_mapping(clean_audit):
    """ONE trading.bridge.completed event per run: built proposal ids, skips
    with reasons, and the full evidence ref->uuid mapping so the interim
    signal_ids stay reconstructible (ADR-0006)."""
    s = clean_audit
    _seed(s)
    _ohlc(s, _instrument(s, "ZBRA"))
    clock = FrozenClock(T0)
    bridged = _memo(s, clock, "ZBRA")
    refless = _memo(s, clock, "ZBRB", refs=())

    report = bridge_memos(s, clock)
    n = s.execute(text(
        "SELECT count(*) FROM audit.decision_events "
        "WHERE event_type = 'trading.bridge.completed'")).scalar()
    assert n == 1
    payload = _bridge_event(s)
    assert payload["built"] == [{
        "memo_id": bridged, "symbol": "ZBRA",
        "proposal_id": report.built[0].proposal_id,
        "verdict": "PASS", "qty": 53}]
    assert payload["skipped"] == [{
        "memo_id": refless, "symbol": "ZBRB",
        "reason": "memo has no evidence refs — no trade without evidence "
                  "(Principle 1)"}]
    assert payload["evidence_signal_ids"] == {
        bridged: {r: str(evidence_signal_id(r)) for r in REFS}}
    # and the mapping reproduces the persisted signal_ids exactly
    row = _proposals(s)[0]
    assert [str(u) for u in row.signal_ids] == list(
        payload["evidence_signal_ids"][bridged].values())
