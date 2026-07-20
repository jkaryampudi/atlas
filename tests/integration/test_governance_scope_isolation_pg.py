"""P0.1 (ADR-0018) req 5: governance paths (promotion / demotion / benchmark /
drawdown gates) can consume ONLY authoritative performance. Proves the fail-closed
boundary and that the existing governance functions take no scope/composite at all
(they read per-strategy validated results), so a shadow / all_simulated / mixed
composite can never reach a governance decision."""
from __future__ import annotations

import inspect
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp import strategy_lifecycle as sl
from atlas.dcp.backtest.approval import evaluate_approval, transition_to_paper
from atlas.dcp.reporting.attribution import authoritative_composite_for_governance
from atlas.dcp.trading.bands import check_bands
from tests.conftest import requires_pg

pytestmark = requires_pg

D1, D2 = date(2026, 7, 13), date(2026, 7, 14)
AS1 = datetime(2026, 7, 13, 23, 0, tzinfo=UTC)
AS2 = datetime(2026, 7, 14, 23, 0, tzinfo=UTC)
CLOCK = FrozenClock(datetime(2026, 7, 15, 2, tzinfo=UTC))


def _seed(s, *, xsmom_d2="1100") -> str:
    s.execute(text("DELETE FROM reporting.attribution_daily"))
    s.execute(text("DELETE FROM trading.portfolio_snapshots"))
    s.execute(text("DELETE FROM quant.strategies "
                   "WHERE family IN ('xsmom-pit-tr', 'pead-sue-tr')"))
    xid = str(s.execute(text(
        "INSERT INTO quant.strategies (family,name,version,spec,code_sha,"
        " tolerance_bands,state) VALUES ('xsmom-pit-tr','n','1.0.0','{}','x','{}',"
        " 'research_shadow') RETURNING id")).scalar())
    s.execute(text(
        "INSERT INTO quant.strategies (family,name,version,spec,code_sha,"
        " tolerance_bands,state) VALUES ('pead-sue-tr','n','1.0.0','{}','p','{}',"
        " 'paper')"))
    for as_of, nav in ((AS1, "3000"), (AS2, "3300")):
        s.execute(text(
            "INSERT INTO trading.portfolio_snapshots (as_of, nav_aud, cash_aud, "
            " holdings, exposures, fx_rates) VALUES (:a,:n,0,'[]','{}','{}')"),
            {"a": as_of, "n": nav})
    for d, sleeve, val, ret, bench in [
            (D1, "xsmom", "1000", None, None), (D2, "xsmom", xsmom_d2, "0.05", "0.01"),
            (D1, "pead", "2000", None, None), (D2, "pead", "2200", "0.10", "0.01")]:
        s.execute(text(
            "INSERT INTO reporting.attribution_daily (session_date, sleeve, "
            " value_aud, ret_1d, benchmark_ret_1d, created_at) "
            "VALUES (:d,:sl,:v,:r,:b,:ca)"),
            {"d": d, "sl": sleeve, "v": val, "r": ret, "b": bench, "ca": AS1})
    s.commit()
    return xid


def test_promotion_rejects_a_research_shadow_strategy(clean_audit):
    """Test 9: promotion consumes a strategy's OWN validated state, never a
    performance composite — a research_shadow strategy cannot be promoted."""
    s = clean_audit
    xid = _seed(s)
    audit = PostgresAuditLog(s, CLOCK)
    with pytest.raises(ValueError, match="not 'validated'"):
        transition_to_paper(s, audit, strategy_id=xid,
                            approved_by="p", decision_ref="ADR-test", clock=CLOCK)
    # and the guarded governance accessor refuses a research_shadow scope outright
    with pytest.raises(ValueError, match="governance calculations require"):
        authoritative_composite_for_governance(s, sl.RESEARCH_SHADOW_SCOPE)


def test_demotion_and_gates_reject_all_simulated_or_mixed_scope(clean_audit):
    """Tests 10, 11: the only composite a governance path may read is the
    authoritative one; all_simulated / research_shadow are refused, and the
    authoritative composite excludes shadow."""
    s = clean_audit
    _seed(s)
    for bad in (sl.ALL_SIMULATED, sl.RESEARCH_SHADOW_SCOPE, "include_all"):
        with pytest.raises(ValueError):
            authoritative_composite_for_governance(s, bad)
    # the authoritative composite is the pead-only number (shadow excluded)...
    auth = authoritative_composite_for_governance(s)
    assert auth == Decimal("9.00")
    # ...and it is byte-identical no matter what the shadow sleeve does
    _seed(s, xsmom_d2="999999999")
    assert authoritative_composite_for_governance(s) == auth


def test_governance_functions_take_no_scope_or_performance_argument():
    """Structural proof: promotion and demotion accept no scope/performance/
    composite parameter — they cannot be handed a shadow composite at all."""
    for fn in (evaluate_approval, transition_to_paper, check_bands):
        params = set(inspect.signature(fn).parameters)
        assert not (params & {"scope", "performance", "composite",
                              "all_simulated", "alpha"}), (fn.__name__, params)
