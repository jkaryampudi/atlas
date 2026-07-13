"""Bridge signal-ref resolution (ADR-0010 wiring): a memo whose evidence refs
include a quant signal ref ('dcp:signal:xsmom:<uuid>:<date>') bridges with
the REAL quant.signals UUID in proposals.signal_ids; other refs keep the
ADR-0006 interim uuid5; a signal-shaped ref with no row fails the memo closed
(never a fabricated lineage). Seeding mirrors test_bridge_pg.py."""
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
T0 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
FX_USD_AUD = Decimal("1.5")
BARS_REF = "dcp:bars:ZBSA:2026-07-13"


def _clean(s) -> None:
    s.execute(text("UPDATE trading.trade_proposals "
                   "SET risk_check_id = NULL, state = 'draft'"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks", "trading.trade_proposals",
              "trading.positions", "trading.portfolio_snapshots"):
        s.execute(text(f"DELETE FROM {t}"))
    s.execute(text("DELETE FROM risk.limit_sets WHERE version > 1"))
    s.execute(text("DELETE FROM quant.sleeve_daily"))
    s.execute(text("DELETE FROM quant.signals"))
    s.execute(text("DELETE FROM quant.strategies WHERE family = 'xsmom-pit-tr'"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZBS%')"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol LIKE 'ZBS%'"))


def _instrument(s, symbol: str):
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, sector_gics, currency) "
        "VALUES (:sym, 'XTEST', 'US', 'stock', :sym, 'Information Technology', "
        "'USD') RETURNING id"), {"sym": symbol}).scalar()


def _ohlc(s, iid, days: int = 21, start: date = date(2026, 6, 23)) -> None:
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid, :d, 100, 101, 99, 100, 1000000, 'EodhdAdapter')"),
        [{"iid": iid, "d": start + timedelta(days=i)} for i in range(days)])


def _memo(s, clock, symbol: str, refs: list[str]) -> str:
    return str(s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, "
        "recommendation, evidence_refs, created_at) "
        "VALUES ('committee', :sym, 'BUY', CAST(:er AS jsonb), :ca) "
        "RETURNING id"),
        {"sym": symbol, "er": json.dumps(refs), "ca": clock.now()}).scalar())


def _seed(s, clock):
    _clean(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD', 'AUD', '2026-07-10', :r, 'test') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = :r"),
        {"r": FX_USD_AUD})
    iid = _instrument(s, "ZBSA")
    _ohlc(s, iid)
    strategy_id = s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, code_sha, "
        " tolerance_bands, state) "
        "VALUES ('xsmom-pit-tr', 'xsmom_pit', '1.0.0', '{}', 'test-sha', "
        "        '{}', 'paper') RETURNING id")).scalar()
    signal_id = s.execute(text(
        "INSERT INTO quant.signals (strategy_id, instrument_id, signal_date, "
        " direction, rank, formation_return, valid_until, created_at) "
        "VALUES (:sid, :iid, '2026-07-13', 'long', 1, 0.5, '2026-07-31', :ca) "
        "RETURNING id"), {"sid": strategy_id, "iid": iid,
                          "ca": clock.now()}).scalar()
    return iid, signal_id


def test_signal_ref_resolves_to_the_real_quant_signal_uuid(clean_audit):
    s = clean_audit
    clock = FrozenClock(T0)
    _, signal_id = _seed(s, clock)
    signal_ref = f"dcp:signal:xsmom:{signal_id}:2026-07-13"
    memo_id = _memo(s, clock, "ZBSA", [signal_ref, BARS_REF])

    report = bridge_memos(s, clock)
    assert len(report.built) == 1 and report.built[0].verdict == "PASS"

    sids = s.execute(text(
        "SELECT signal_ids FROM trading.trade_proposals WHERE id = :p"),
        {"p": report.built[0].proposal_id}).scalar_one()
    # REAL signal uuid first (memo ref order), interim uuid5 for the bars ref
    assert [str(x) for x in sids] == [str(signal_id),
                                      str(evidence_signal_id(BARS_REF))]

    payload = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'trading.bridge.completed' "
        "ORDER BY seq DESC LIMIT 1")).scalar_one()
    assert payload["evidence_signal_ids"][memo_id] == {
        signal_ref: str(signal_id),
        BARS_REF: str(evidence_signal_id(BARS_REF))}


def test_forged_signal_ref_fails_the_memo_closed(clean_audit):
    s = clean_audit
    clock = FrozenClock(T0)
    _seed(s, clock)
    ghost = uuid.uuid4()
    _memo(s, clock, "ZBSA", [f"dcp:signal:xsmom:{ghost}:2026-07-13", BARS_REF])

    report = bridge_memos(s, clock)
    assert not report.built and len(report.skipped) == 1
    assert "no such row" in report.skipped[0].reason
    assert str(ghost) in report.skipped[0].reason
    assert s.execute(text(
        "SELECT count(*) FROM trading.trade_proposals")).scalar() == 0


def test_memo_without_signal_refs_keeps_the_interim_uuid5(clean_audit):
    s = clean_audit
    clock = FrozenClock(T0)
    _seed(s, clock)
    _memo(s, clock, "ZBSA", [BARS_REF, "dcp:indicators:ZBSA:2026-07-13"])

    report = bridge_memos(s, clock)
    assert len(report.built) == 1
    sids = s.execute(text(
        "SELECT signal_ids FROM trading.trade_proposals WHERE id = :p"),
        {"p": report.built[0].proposal_id}).scalar_one()
    assert [str(x) for x in sids] == [
        str(evidence_signal_id(BARS_REF)),
        str(evidence_signal_id("dcp:indicators:ZBSA:2026-07-13"))]
