"""Full-universe implementable-variant mode (--top-universe 0; ADR-0016
evidence run) on atlas_test with seeded fixtures — the pg smoke for the
impl500 path: load_impl_context(top_universe=0) skips the dollar-volume matrix
entirely, the base equals the whole eligible set at EVERY rebalance, the trial
registers under the NEW family `xsmom-impl500-tr`, the audit event names the
full-universe run, and pead/combined are refused (ADR-0015: PEAD budget 0).

The fixture world is imported from test_impl_variant_pg — the SAME eight
members, so the two integration files describe one world. Every fixture row is
written INSIDE the test transaction (rolled back at teardown)."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.backtest.impl_variant_run import (
    load_impl_context,
    run_impl_variant,
)
from atlas.dcp.backtest.registry import trial_count
from tests.conftest import requires_pg
from tests.integration.test_impl_variant_pg import MEMBERS, _seed

pytestmark = requires_pg


def test_impl500_runner_full_path(pg_session):
    s = pg_session
    _seed(s)
    s.execute(text("DELETE FROM quant.trial_registry "
                   "WHERE strategy_family LIKE '%impl500%'"))   # in-txn only
    audit = PostgresAuditLog(s, FrozenClock(datetime(2013, 12, 31, 22,
                                                     tzinfo=UTC)))
    ctx = load_impl_context(s, top_universe=0)
    assert ctx.sleeves.adv.top_universe == 0
    assert set(ctx.members) == set(MEMBERS)

    run = run_impl_variant(s, audit, ctx, variant="xsmom", paths=8, seed=7)
    assert run.family == "xsmom-impl500-tr"
    assert trial_count(s, "xsmom-impl500-tr") == 1
    # no screen: the base IS the eligible set at every rebalance
    assert run.counts and all(c.base == c.eligible for c in run.counts)
    g = run.gate
    assert 0.0 <= g.null_p_value <= 1.0
    assert isinstance(g.passed, bool)
    assert run.endpoints and run.endpoints[-1].endpoint == ctx.panel.dates[-1]

    # the registered spec and the audit trail must NAME the full universe —
    # an impl500 row that read like the screened run would poison the registry
    spec_universe = s.execute(text(
        "SELECT count(*) FROM audit.decision_events "
        "WHERE event_type = 'quant.backtest.completed' "
        "AND actor_id = 'impl_variant_run' "
        "AND entity_id = 'xsmom-impl500-tr/portfolio' "
        "AND payload->>'universe' LIKE '%no liquidity screen%'")).scalar()
    assert spec_universe == 1

    # ADR-0015: no impl500 family exists for pead/combined — refused loudly
    for variant in ("pead", "combined"):
        with pytest.raises(ValueError, match="ADR-0015"):
            run_impl_variant(s, audit, ctx, variant=variant, paths=8, seed=7)
