"""P0.1 objective 2 (ADR-0018): an end-to-end proof that a research_shadow
strategy, run through the COMPLETE T0-T9 daily cycle, can create no authoritative
proposal / order / fill / approved-sleeve position; its attribution sleeve stays
empty and labelled; and the blocked bridge action is on the audit chain. Mirrors
the test_daily_cycle_pg harness with a FRESH memo (so the memo is a real bridge
candidate that is then refused at signal resolution, not merely stale)."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.adapters.fixture import FixtureAdapter
from atlas.dcp.risk.seed_limits import seed_limit_set
from atlas.ops.daily import run_daily_cycle
from tests.conftest import requires_pg

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
RUN = datetime(2026, 7, 13, 23, 30, tzinfo=UTC)      # post-close, passes refusal
SIG_DATE = date(2026, 7, 13)
FX = Decimal("1.5")

NODES = ["t0_ingest", "t1_verify_chain", "t2_expire", "t3_settle", "t4_stops",
         "t5_snapshot", "t5b_bands", "t5c_cusum", "t6_reconcile", "t6b_signals",
         "t6c_pead_signals", "t7_desk", "t8_bridge", "t8b_attribution",
         "t8c_core", "t9_report", "t9b_brief"]


def _clean(s) -> None:
    s.execute(text("UPDATE trading.trade_proposals SET risk_check_id = NULL, "
                   "state = 'draft'"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks", "trading.trade_proposals",
              "trading.positions", "trading.portfolio_snapshots",
              "trading.reconciliations", "reporting.attribution_daily",
              "reporting.morning_brief", "quant.sleeve_daily", "quant.signals",
              "research.memos"):
        s.execute(text(f"DELETE FROM {t}"))
    s.execute(text("DELETE FROM workflow.workflow_node_results "
                   "WHERE run_id LIKE 'daily-%'"))
    s.execute(text("DELETE FROM workflow.workflow_runs WHERE run_id LIKE 'daily-%'"))
    s.execute(text("DELETE FROM quant.strategies "
                   "WHERE family IN ('xsmom-pit-tr', 'pead-sue-tr')"))
    s.execute(text("DELETE FROM risk.limit_sets WHERE version > 1"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZSH%')"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol LIKE 'ZSH%'"))
    s.execute(text("UPDATE market.instruments SET is_active = false "
                   "WHERE symbol IN ('SPY', 'INDA')"))


def _seed_research_shadow(s, clock) -> None:
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, sector_gics, currency) VALUES "
        "('ZSHA','XTEST','US','stock','ZSHA','Information Technology','USD') "
        "RETURNING id")).scalar()
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid, :d, 100, 101, 99, 100, 1000000, 'EodhdAdapter')"),
        [{"iid": iid, "d": date(2026, 6, 23) + timedelta(days=i)}
         for i in range(21)])
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD','AUD','2026-07-10',:r,'zsh-test') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = :r"), {"r": FX})
    # the DOWNGRADED strategy + its signal (state research_shadow, not paper/live)
    strategy_id = s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, code_sha, "
        " tolerance_bands, state) VALUES ('xsmom-pit-tr','xsmom_pit','1.0.0',"
        " '{}','sha','{}','research_shadow') RETURNING id")).scalar()
    signal_id = s.execute(text(
        "INSERT INTO quant.signals (strategy_id, instrument_id, signal_date, "
        " direction, rank, formation_return, valid_until, created_at) "
        "VALUES (:sid, :iid, :d, 'long', 1, 0.5, '2026-08-31', :ca) RETURNING id"),
        {"sid": strategy_id, "iid": iid, "d": SIG_DATE, "ca": clock.now()}).scalar()
    # a FRESH BUY memo citing that signal (created_at = run clock -> in the 48h
    # candidacy window, so it is a real bridge candidate, not a stale skip)
    s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, "
        "recommendation, evidence_refs, created_at) "
        "VALUES ('committee','ZSHA','BUY', CAST(:er AS jsonb), :ca)"),
        {"er": json.dumps([f"dcp:signal:xsmom:{signal_id}:{SIG_DATE.isoformat()}",
                           "dcp:bars:ZSHA:2026-07-13"]), "ca": clock.now()})


def test_research_shadow_blocked_through_full_daily_cycle(clean_audit):
    s = clean_audit
    _clean(s)
    clock = FrozenClock(RUN)
    _seed_research_shadow(s, clock)

    results = run_daily_cycle(s, clock, FixtureAdapter(FIXTURES))

    # the full cycle ran every node
    assert list(results.keys()) == NODES
    # the downgraded strategy generates no signals (excluded from paper/live)
    assert results["t6b_signals"] == ("signals idle (no paper/live "
                                      "xsmom-pit-tr strategy)")
    # the fresh memo WAS a candidate but the bridge refused it (not stale=0)
    assert results["t8_bridge"] == "bridged 0 (none) · skipped 1"

    # no authoritative proposal / order / fill / position exists anywhere
    for tbl in ("trading.trade_proposals", "trading.orders",
                "trading.executions", "trading.tax_lots", "trading.positions"):
        assert s.execute(text(f"SELECT count(*) FROM {tbl}")).scalar() == 0, tbl

    # the block is auditable: one trading.bridge.completed event whose skip
    # reason names research_shadow / no capital
    payload = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'trading.bridge.completed' "
        "ORDER BY seq DESC LIMIT 1")).scalar_one()
    assert len(payload["skipped"]) == 1
    reason = payload["skipped"][0]["reason"]
    assert "research_shadow" in reason and "no capital" in reason

    # research-only attribution stays clearly non-earning: the xsmom sleeve
    # holds no lots, so its stored value is zero (never a fabricated return)
    xsmom_vals = [r[0] for r in s.execute(text(
        "SELECT value_aud FROM reporting.attribution_daily "
        "WHERE sleeve = 'xsmom'")).all()]
    assert all(Decimal(v) == 0 for v in xsmom_vals)
