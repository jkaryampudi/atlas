"""Implementable-variant runner on atlas_test with seeded fixtures (no live
calls): the dollar-volume basis pin (the vendor stores volume ALREADY
split-adjusted while closes are raw — the product with the split-adjusted
close must equal true traded dollars, NOT a double-adjusted figure), and a
full-path smoke through load_impl_context + run_impl_variant for all three
variants: trials registered per family, the audit event, gate report shapes,
and the SPY-dividend fail-loud rule inherited from the TR loader.

Every fixture row is written INSIDE the test transaction (rolled back at
teardown), so the panel sees exactly this test's world."""
from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.backtest.impl_variant_run import (
    load_dollar_volume,
    load_impl_context,
    run_impl_variant,
)
from atlas.dcp.backtest.portfolio import PricePanel
from atlas.dcp.backtest.registry import trial_count
from atlas.dcp.market_data.calendars import trading_days_between
from tests.conftest import requires_pg

pytestmark = requires_pg

SESSIONS = trading_days_between("US", date(2011, 1, 3), date(2013, 12, 31))
MEMBERS = [f"ZIV{k}" for k in range(8)]
FETCHED = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)
EPOCH = date(2005, 1, 3)


def _instrument(s, symbol: str, *, active: bool) -> str:
    existing = s.execute(text(
        "SELECT id FROM market.instruments WHERE symbol = :sym"),
        {"sym": symbol}).scalar()
    if existing is not None:
        s.execute(text("UPDATE market.instruments SET is_active = :act "
                       "WHERE id = :iid"), {"iid": existing, "act": active})
        return str(existing)
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, sector_gics, currency, is_active) "
        "VALUES (:sym, 'XTEST', 'US', 'stock', :sym, 'Broad', 'USD', :act) "
        "RETURNING id"), {"sym": symbol, "act": active}).scalar())


def _bars(s, iid: str, rate: float, *, volume: int = 1_000_000) -> None:
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid, :d, :c, :c, :c, :c, :v, 'EodhdAdapter')"),
        [{"iid": iid, "d": d, "c": round(100.0 * math.exp(rate * i), 6),
          "v": volume} for i, d in enumerate(SESSIONS)])


def _member_row(s, ticker: str) -> None:
    s.execute(text(
        "INSERT INTO validation.index_membership "
        "(index_code, ticker, name, start_date, end_date, is_active_now, "
        " is_delisted, fetched_at) "
        "VALUES ('GSPC.INDX', :t, :t, :sd, NULL, TRUE, FALSE, :f)"),
        {"t": ticker, "sd": EPOCH, "f": FETCHED})


def _dividend(s, iid: str, ex: date, amount: str) -> None:
    s.execute(text(
        "INSERT INTO market.corporate_actions (instrument_id, action_date, "
        "action_type, amount, currency, source) "
        "VALUES (:iid, :d, 'dividend', :a, 'USD', 'test')"),
        {"iid": iid, "d": ex, "a": amount})


_FPES = (date(2011, 3, 31), date(2011, 6, 30), date(2011, 9, 30),
         date(2011, 12, 31), date(2012, 3, 31), date(2012, 6, 30),
         date(2012, 9, 30), date(2012, 12, 31), date(2013, 3, 31),
         date(2013, 6, 30))


def _surprises(s, iid: str, *, beats: bool) -> None:
    """Ten quarterly reports through 2011-2013 — enough priors that SUE is
    defined from mid-2012 on; `beats` fixes the surprise sign, and a small
    per-quarter wobble keeps the prior-surprise stdev non-zero (a constant
    surprise would leave SUE undefined by the zero-stdev rule)."""
    base = Decimal("1.10") if beats else Decimal("0.90")
    for k, fpe in enumerate(_FPES):
        s.execute(text(
            "INSERT INTO market.earnings_surprises (instrument_id, "
            "fiscal_period_end, report_date, eps_actual, eps_estimate, "
            "surprise_pct, currency, before_after_market, source, fetched_at) "
            "VALUES (:iid, :fpe, :rd, :a, '1.00', :sp, 'USD', 'BeforeMarket', "
            "'test', :fa)"),
            {"iid": iid, "fpe": fpe, "rd": fpe + timedelta(days=30),
             "a": str(base + Decimal("0.01") * (k % 3)),
             "sp": str((base - 1) * 100), "fa": FETCHED})


def _seed(s) -> None:
    s.execute(text("DELETE FROM market.price_bars_daily"))       # in-txn only
    s.execute(text("DELETE FROM market.corporate_actions"))
    s.execute(text("DELETE FROM market.earnings_surprises"))
    s.execute(text("DELETE FROM validation.index_membership"))
    s.execute(text("DELETE FROM quant.trial_registry WHERE lineage IN "
                   "('momentum', 'pead', 'momentum+pead')"))  # ADR-0016
    spy = _instrument(s, "SPY", active=True)
    _bars(s, spy, 0.0004)
    _dividend(s, spy, date(2012, 9, 21), "0.77")   # TR loader fails without it
    for k, sym in enumerate(MEMBERS):
        iid = _instrument(s, sym, active=False)
        _bars(s, iid, -0.0004 + 0.0002 * k, volume=(k + 1) * 1_000_000)
        _member_row(s, sym)
        _surprises(s, iid, beats=(k % 2 == 0))


# ------------------------------------------------ dollar-volume basis pin ---

def test_dollar_volume_is_adjusted_close_times_stored_volume(pg_session):
    """A 2:1 split mid-series: stored closes are RAW (100 before, 50 after);
    raw traded shares are 1,000 before and 2,000 after, so the vendor serves
    volume ALREADY adjusted to the post-split basis — 2,000 on EVERY day.
    True traded dollars are 100,000 every day. The loader must produce
    exactly that (split factors cancel: adjusted close 50 x stored 2,000);
    using the engine's re-adjusted OBar volume instead would double the
    pre-split half to 200,000."""
    s = pg_session
    s.execute(text("DELETE FROM market.price_bars_daily"))
    s.execute(text("DELETE FROM market.corporate_actions"))
    iid = _instrument(s, "ZSPLIT", active=False)
    split_day = SESSIONS[30]
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid, :d, :c, :c, :c, :c, :v, 'EodhdAdapter')"),
        [{"iid": iid, "d": d,
          "c": 100.0 if d < split_day else 50.0,
          "v": 2_000}
         for d in SESSIONS[:60]])
    s.execute(text(
        "INSERT INTO market.corporate_actions (instrument_id, action_date, "
        "action_type, ratio, source) VALUES (:iid, :d, 'split', 2, 'test')"),
        {"iid": iid, "d": split_day})

    dates = SESSIONS[:60]
    panel = PricePanel(dates=dates,
                       opens={"ZSPLIT": [1.0] * 60},
                       closes={"ZSPLIT": [1.0] * 60})
    dv = load_dollar_volume(s, ["ZSPLIT"], panel)["ZSPLIT"]
    assert all(x == pytest.approx(100_000.0) for x in dv)


# ------------------------------------------------------- full-path smoke ---

def test_impl_runner_full_path(pg_session):
    s = pg_session
    _seed(s)
    audit = PostgresAuditLog(s, FrozenClock(datetime(2013, 12, 31, 22,
                                                     tzinfo=UTC)))
    ctx = load_impl_context(s)
    assert set(ctx.members) == set(MEMBERS)          # SPY outside the universe
    assert ctx.coverage.symbols_with_reports == len(MEMBERS)

    runs = {v: run_impl_variant(s, audit, ctx, variant=v, paths=8, seed=7)
            for v in ("xsmom", "pead", "combined")}

    for v, run in runs.items():
        assert run.family == f"{v}-impl-tr"
        assert trial_count(s, run.family) == 1
        g = run.gate
        assert 0.0 <= g.null_p_value <= 1.0
        assert isinstance(g.passed, bool)
        assert run.endpoints and run.endpoints[-1].endpoint == ctx.panel.dates[-1]
        # every rebalance base is capped by the universe and the pead base
        # is a subset of it
        for c in run.counts:
            assert c.base <= c.eligible <= len(MEMBERS)
            assert c.pead_base <= c.base
    # the sleeves hold at most SLEEVE_N names a side: combined turnover exists
    assert runs["combined"].run.result.n_rebalances > 0

    ev = s.execute(text(
        "SELECT count(*) FROM audit.decision_events "
        "WHERE event_type = 'quant.backtest.completed' "
        "AND actor_id = 'impl_variant_run'")).scalar()
    assert ev == 3


def test_tr_loader_fails_loud_without_spy_dividends(pg_session):
    """The inherited ADR-0009 defence: a total-return run whose SPY carries no
    yield would re-create the original benchmark defect — refused."""
    s = pg_session
    _seed(s)
    s.execute(text(
        "DELETE FROM market.corporate_actions WHERE action_type='dividend'"))
    with pytest.raises(RuntimeError, match="no stored dividends"):
        load_impl_context(s)
