"""ADR-0010 tolerance-band check (atlas/dcp/trading/bands.py).

The sleeve is built through the REAL lifecycle (build -> approve -> settle,
seeding mirrors test_daily_cycle_pg.py) so the attribution join — tax lot ->
execution -> order -> proposal.signal_ids && strategy signals — is exercised
end to end, then the series is FORGED to the breach points:

Sizing (empty A$100k book, entry 100, stop 95, fx 1.5): L1 weight cap binds
-> 53 shares; fill at the 7-14 session open 102. With the 7-15 close forced
to 45: sleeve MV = 53 x 45 x 1.5 = A$3577.50, realised 0.

- DD breach: forged prior peak 7155 -> drawdown 3577.5/7155 - 1 = -0.50
  < -0.40 -> demote to 'suspended', audit event, alert, LATCH (second run
  records but never re-demotes).
- Excess breach: 126 forged prior sessions at value 3600 / SPY TR 100, today
  3577.50 / SPY TR 130 -> excess = (3577.5/3600 - 1)*100 - 30 = -30.625pp
  < -25 -> demote (dd -0.00625 stays inside its band).
- Dormancy: 125 prior sessions -> excess is NULL, nothing fires.
- Empty sleeve: value NULL, no breach possible.
- Malformed bands on a paper strategy: RuntimeError (governance breach).
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

import atlas.dcp.trading.bands as bands_mod
from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.risk.seed_limits import seed_limit_set
from atlas.dcp.trading.bands import check_bands
from atlas.dcp.trading.proposals import approve, build_proposal, settle_orders
from tests.conftest import requires_pg

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
T0 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
T15 = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)
FX_USD_AUD = Decimal("1.5")
BANDS = {"max_drawdown_from_sleeve_peak": -0.40,
         "trailing_126_session_excess_vs_spy_tr_pp": -25.0,
         "demote_to": "suspended", "provisional": True}
SLEEVE_TODAY = Decimal("3577.50")           # 53 x 45 x 1.5 (module docstring)


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
                   "(SELECT id FROM market.instruments "
                   " WHERE symbol LIKE 'ZBD%' OR symbol = 'ZSPY')"))
    s.execute(text("DELETE FROM market.instruments "
                   "WHERE symbol LIKE 'ZBD%' OR symbol = 'ZSPY'"))


def _strategy(s, bands: dict | None = None):
    return s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, code_sha, "
        " tolerance_bands, state) "
        "VALUES ('xsmom-pit-tr', 'xsmom_pit', '1.0.0', '{}', 'test-sha', "
        "        CAST(:b AS jsonb), 'paper') RETURNING id"),
        {"b": json.dumps(BANDS if bands is None else bands)}).scalar()


def _entered_sleeve(s, clock) -> str:
    """A held 53-share ZBDA position whose lot traces to a proposal carrying
    the strategy's REAL signal uuid (the attribution join under test)."""
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    strategy_id = _strategy(s)
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, sector_gics, currency) "
        "VALUES ('ZBDA', 'XTEST', 'US', 'stock', 'ZBDA', "
        "'Information Technology', 'USD') RETURNING id")).scalar()
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid, :d, 100, 101, 99, 100, 1000000, 'EodhdAdapter')"),
        [{"iid": iid, "d": date(2026, 6, 23) + timedelta(days=i)}
         for i in range(21)])
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD','AUD','2026-07-10',:r,'test'), "
        "       ('USD','AUD','2026-07-14',:r,'test'), "
        "       ('USD','AUD','2026-07-15',:r,'test') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = :r"),
        {"r": FX_USD_AUD})
    signal_id = s.execute(text(
        "INSERT INTO quant.signals (strategy_id, instrument_id, signal_date, "
        " direction, rank, formation_return, valid_until, created_at) "
        "VALUES (:sid, :iid, '2026-07-13', 'long', 1, 0.5, '2026-07-31', :ca) "
        "RETURNING id"), {"sid": strategy_id, "iid": iid,
                          "ca": clock.now()}).scalar()
    memo_id = str(s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, "
        "recommendation, evidence_refs, created_at) "
        "VALUES ('committee', 'ZBDA', 'BUY', '[]', :ca) RETURNING id"),
        {"ca": clock.now()}).scalar())
    res = build_proposal(s, clock, memo_id=memo_id, symbol="ZBDA",
                         signal_refs=[str(signal_id)],
                         entry_price=Decimal("100"), stop_price=Decimal("95"),
                         target_price=Decimal("120"))
    assert res.state == "pending_approval" and res.qty == 53
    clock.advance_to(T0 + timedelta(hours=1))
    assert approve(s, clock, proposal_id=res.proposal_id,
                   acknowledged_risks=True).status == "approved"
    # 7-14 fills the entry at open 102; 7-15 closes at 45 (the forged crash)
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) VALUES "
        "(:iid, '2026-07-14', 102, 104, 101, 103, 1000000, 'EodhdAdapter'), "
        "(:iid, '2026-07-15', 46, 47, 44, 45, 1000000, 'EodhdAdapter')"),
        {"iid": iid})
    clock.advance_to(T15)
    assert len(settle_orders(s, clock)) == 1
    return str(strategy_id)


def _forge_prior(s, strategy_id: str, sessions: list[date], *, value: str,
                 peak: str, spy: str | None) -> None:
    s.execute(text(
        "INSERT INTO quant.sleeve_daily (strategy_id, session_date, "
        " sleeve_value, spy_tr_close, peak_value, drawdown, created_at) "
        "VALUES (:sid, :d, :v, :spy, :pk, 0, :ca)"),
        [{"sid": strategy_id, "d": d, "v": value, "spy": spy, "pk": peak,
          "ca": T0} for d in sessions])


def _seed_spy(s, close: str) -> None:
    s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, sector_gics, currency) "
        "VALUES ('SPY', 'XTEST', 'US', 'etf', 'SPY', NULL, 'USD') "
        "ON CONFLICT (symbol, exchange) DO NOTHING"))
    iid = s.execute(text(
        "SELECT id FROM market.instruments WHERE symbol = 'SPY' LIMIT 1")).scalar()
    s.execute(text(
        "DELETE FROM market.price_bars_daily "
        "WHERE instrument_id = :iid AND bar_date = '2026-07-15'"), {"iid": iid})
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid, '2026-07-15', :c, :c, :c, :c, 1000, 'EodhdAdapter')"),
        {"iid": iid, "c": close})


@pytest.fixture
def alerts(monkeypatch):
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        bands_mod, "notify",
        lambda title, msg, *, priority="default": sent.append(
            (title, msg, priority)) or True)
    return sent


def _demotions(s) -> list[dict]:
    return [r[0] for r in s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'quant.strategy.demoted' ORDER BY seq")).all()]


def _state(s, strategy_id: str) -> str:
    return s.execute(text("SELECT state FROM quant.strategies WHERE id = :i"),
                     {"i": strategy_id}).scalar_one()


# -------------------------------------------------------------------- empty

def test_empty_sleeve_records_null_and_never_breaches(clean_audit, alerts):
    s = clean_audit
    _clean(s)
    sid = _strategy(s)
    report = check_bands(s, FrozenClock(T15))
    assert [st.action for st in report.statuses] == ["empty"]
    row = s.execute(text(
        "SELECT sleeve_value, peak_value, drawdown, excess_126s_pp "
        "FROM quant.sleeve_daily WHERE strategy_id = :sid"),
        {"sid": sid}).one()
    assert row == (None, None, None, None)
    assert _state(s, str(sid)) == "paper"
    assert not _demotions(s) and not alerts


# ----------------------------------------------------------------- DD breach

def test_dd_breach_demotes_latches_audits_and_alerts(clean_audit, alerts):
    s = clean_audit
    _clean(s)
    clock = FrozenClock(T0)
    sid = _entered_sleeve(s, clock)
    _forge_prior(s, sid, [date(2026, 7, 14)], value="7155", peak="7155",
                 spy=None)

    report = check_bands(s, FrozenClock(T15))
    assert [st.action for st in report.statuses] == ["demoted"]
    assert _state(s, sid) == "suspended"

    row = s.execute(text(
        "SELECT sleeve_value, peak_value, drawdown, excess_126s_pp "
        "FROM quant.sleeve_daily WHERE strategy_id = :sid "
        "AND session_date = '2026-07-15'"), {"sid": sid}).one()
    assert Decimal(row.sleeve_value) == SLEEVE_TODAY
    assert Decimal(row.peak_value) == Decimal("7155")
    assert float(row.drawdown) == pytest.approx(-0.5)
    assert row.excess_126s_pp is None       # dormant: no 126-session base

    events = _demotions(s)
    assert len(events) == 1
    p = events[0]
    assert p["dd_breach"] is True and p["excess_breach"] is False
    assert p["drawdown"] == pytest.approx(-0.5) and p["dd_limit"] == -0.4
    assert p["from_state"] == "paper" and p["to_state"] == "suspended"
    assert p["latching"] is True
    assert len(alerts) == 1 and alerts[0][2] == "high"
    assert "DEMOTED" in alerts[0][0]

    # LATCH: the next session records the series but never re-demotes
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "SELECT i.id, '2026-07-16', 45, 46, 44, 45, 1000000, 'EodhdAdapter' "
        "FROM market.instruments i WHERE i.symbol = 'ZBDA'"))
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD','AUD','2026-07-16',:r,'test') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = :r"),
        {"r": FX_USD_AUD})
    again = check_bands(s, FrozenClock(datetime(2026, 7, 16, 22, 0, tzinfo=UTC)))
    assert [st.action for st in again.statuses] == ["latched"]
    assert _state(s, sid) == "suspended"
    assert len(_demotions(s)) == 1 and len(alerts) == 1


# ------------------------------------------------------------- excess breach

def test_excess_breach_demotes_once_126_sessions_exist(clean_audit, alerts):
    s = clean_audit
    _clean(s)
    clock = FrozenClock(T0)
    sid = _entered_sleeve(s, clock)
    _seed_spy(s, "130")
    prior = trading_days_between("US", date(2025, 12, 1), date(2026, 7, 14))[-126:]
    _forge_prior(s, sid, prior, value="3600", peak="3600", spy="100")

    report = check_bands(s, FrozenClock(T15))
    assert [st.action for st in report.statuses] == ["demoted"]
    st = report.statuses[0]
    # sleeve -0.625% vs SPY TR +30% -> -30.625pp, past the -25pp band
    assert st.excess_pp == pytest.approx(-30.625)
    assert st.drawdown == pytest.approx(-0.00625)   # inside the DD band
    p = _demotions(s)[0]
    assert p["excess_breach"] is True and p["dd_breach"] is False
    assert p["excess_126s_pp"] == pytest.approx(-30.625)
    assert _state(s, sid) == "suspended"
    assert len(alerts) == 1


def test_excess_stays_dormant_below_126_sessions(clean_audit, alerts):
    s = clean_audit
    _clean(s)
    clock = FrozenClock(T0)
    sid = _entered_sleeve(s, clock)
    _seed_spy(s, "130")
    prior = trading_days_between("US", date(2025, 12, 1), date(2026, 7, 14))[-125:]
    _forge_prior(s, sid, prior, value="3600", peak="3600", spy="100")

    report = check_bands(s, FrozenClock(T15))
    st = report.statuses[0]
    assert st.excess_pp is None and st.action == "ok"
    assert _state(s, sid) == "paper"
    assert not _demotions(s) and not alerts


# ------------------------------------------------------------- governance

def test_malformed_bands_on_a_paper_strategy_raise(clean_audit):
    s = clean_audit
    _clean(s)
    _strategy(s, bands={})
    with pytest.raises(RuntimeError, match="tolerance_bands"):
        check_bands(s, FrozenClock(T15))
