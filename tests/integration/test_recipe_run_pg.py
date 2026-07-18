"""THE EQUIVALENCE PIN (Research Factory phase 1 — the load-bearing tests)
plus registration-before-run DURABILITY, the pre-committed kill pair, and
the report.

On the SAME seeded fixture world the implementable-variant tests use
(imported from test_impl_variant_pg — one world, several files), the recipe
{rank momentum_12_1, top 5, monthly, pit-sp500} must reproduce the
impl-variant xsmom full-universe gauntlet path BYTE-IDENTICALLY:

  * the equity curve, float for float (dataclass equality, no tolerance);
  * every gate number (null p, DSR at the same lineage count, SPY TR, EW,
    verdict, reasons verbatim);
  * the seeded monkey-null draws, path by path (same rng convention, same
    sorted eligible sets, same construction);
  * every walk-forward fold and every endpoint verdict.

SCOPE, stated honestly — the fixture precondition is LOAD-BEARING: the pin
holds because no fixture member pays a dividend, so the total-return panel
transform is the exact identity (factor 1.0) on every ranked series and the
price-basis store values coincide with the TR-panel ranking values
float-for-float (_assert_no_member_dividends asserts it;
test_no_dividend_precondition_is_load_bearing proves one member dividend
breaks the coincidence). On real dividend-paying data the recipe
DELIBERATELY ranks on the store's price basis — the live generator's math —
and is then a DIFFERENT trial from the impl-variant runner, not a
reproduction of it: a ranking-basis divergence there is NOT a store bug and
must never be "fixed" by rebasing the store on TR closes (that would break
the genuine store-vs-live-generator pin below). The store-lies rule is
scoped to VALUE mismatches: if the store diverges from the LIVE GENERATOR's
values, fix the store side, never the production side.

Also pinned here: materialized feature values == the production ranking
values on the same panel (view-close formation returns at every rebalance,
exact floats) == the live generator's _formation_returns at a signal
session.

Registration durability: run_recipe COMMITS each trial registration (and
each leg's metrics) in its own transaction, so the crash tests verify from
a SECOND independent session — the only vantage the guarantee is real from
— and this module scrubs its own committed families around every test.

Every fixture row is written INSIDE the test transaction (rolled back at
teardown); the committed registry rows are the deliberate, scrubbed
exception."""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.backtest.impl_variant_run import (
    SLEEVE_N,
    impl_null_results,
    load_impl_context,
    run_impl_variant,
)
from atlas.dcp.backtest.portfolio import PanelView, month_end_indices
from atlas.dcp.backtest.real_run import COSTS
from atlas.dcp.backtest.registry import trial_count
from atlas.dcp.backtest.xsmom_pit_run import pit_eligible
from atlas.dcp.factory import recipe_run as recipe_run_mod
from atlas.dcp.factory.features import get_rank_feature
from atlas.dcp.factory.recipe_run import (
    recipe_null_results,
    render_recipe_report,
    run_recipe,
    run_recipe_gauntlet,
)
from atlas.dcp.factory.spec import RecipeSpec
from atlas.dcp.features.store import feature_panel
from atlas.dcp.features.momentum import compute_momentum
from atlas.dcp.signals.xsmom.generate import _formation_returns
from atlas.dcp.signals.xsmom.v1 import LOOKBACK, SKIP
from tests.conftest import URL, requires_pg
from tests.integration.test_impl_variant_pg import MEMBERS, _seed

pytestmark = requires_pg

CLOCK = FrozenClock(datetime(2013, 12, 31, 22, tzinfo=UTC))
PATHS, SEED = 8, 7

SPEC = RecipeSpec(
    name="mom-12-1-top5", rank_feature="momentum_12_1", direction="desc",
    top_n=5, rebalance="monthly", universe="pit-sp500", lineage="momentum",
    rationale="Winners keep winning: 12-1 cross-sectional momentum persists "
              "(Jegadeesh-Titman 1993); the live book trades its top-5.",
    kill_start=date(2013, 1, 2))


def _scrub_committed_recipe_rows() -> None:
    """run_recipe COMMITS its registrations (count honesty is durability):
    those rows outlive the pg_session rollback, so this module cleans its
    own committed families — and nothing else — before and after each test."""
    engine = create_engine(URL)
    try:
        with engine.begin() as c:
            c.execute(text(
                "DELETE FROM quant.trial_registry "
                "WHERE strategy_family LIKE :fam"),
                {"fam": f"{SPEC.family()}%"})
    finally:
        engine.dispose()


def _committed_momentum_count() -> int:
    """The momentum-lineage count as an INDEPENDENT connection sees it —
    committed rows only. Other test files legitimately COMMIT registry rows
    (e.g. test_trial_registry_pg), so the durability tests assert DELTAS
    against this baseline, never absolute counts."""
    engine = create_engine(URL)
    try:
        with engine.connect() as c:
            return int(c.execute(text(
                "SELECT count(*) FROM quant.trial_registry "
                "WHERE lineage = 'momentum'")).scalar() or 0)
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _committed_registry_isolation(pg_session):
    # depends on pg_session so the test database exists before the scrub
    _scrub_committed_recipe_rows()
    yield
    _scrub_committed_recipe_rows()


def _assert_no_member_dividends(s) -> None:
    """The documented precondition for byte-identity between the price-basis
    store values and the TR-panel ranking values (module docstring)."""
    n = s.execute(text(
        "SELECT count(*) FROM market.corporate_actions ca "
        "JOIN market.instruments i ON i.id = ca.instrument_id "
        "WHERE ca.action_type = 'dividend' AND i.symbol <> 'SPY'")).scalar()
    assert n == 0


# --------------------------------------------------- THE EQUIVALENCE PIN ---

def test_recipe_reproduces_impl_variant_gauntlet_byte_identical(pg_session):
    s = pg_session
    _seed(s)
    _assert_no_member_dividends(s)
    audit = PostgresAuditLog(s, CLOCK)
    assert SLEEVE_N == SPEC.top_n == 5     # the live book shape, both sides

    # the production side: impl-variant xsmom, FULL point-in-time eligible
    # set (top_universe=0 — structurally no liquidity screen), the exact
    # construction the recipe grammar describes
    ctx = load_impl_context(s, top_universe=0)
    impl = run_impl_variant(s, audit, ctx, variant="xsmom", paths=PATHS,
                            seed=SEED)

    # reset the lineage so the recipe's gate deflates at the SAME n_trials
    s.execute(text(
        "DELETE FROM quant.trial_registry WHERE lineage = 'momentum'"))
    rec = run_recipe(s, audit, SPEC, clock=CLOCK, paths=PATHS, seed=SEED)

    assert rec.n_trials == impl.n_trials == 1
    assert rec.start == impl.start

    # equity curve + forced liquidations + unfilled buys: EXACT equality
    # (dataclass ==, float for float — no tolerance anywhere in this test)
    assert rec.run == impl.run
    # both benchmarks through the identical engine: exact
    assert rec.spy == impl.spy
    assert rec.ew == impl.ew
    # every gate number and the verdict with its verbatim reasons: exact
    assert rec.gate == impl.gate
    # every purged walk-forward fold, strategy and SPY alike: exact
    assert rec.wf == impl.wf
    assert rec.wf_spy == impl.wf_spy
    # the full endpoint exhibit (null p and DSR at every rollback): exact
    assert rec.endpoints == impl.endpoints

    # the seeded monkey-null draws, path by path: same rng convention, same
    # sorted eligible sets, same construction => identical FULL results
    nulls_rec = recipe_null_results(ctx.panel, ctx.members,
                                    top_n=SPEC.top_n, paths=PATHS, seed=SEED,
                                    start=impl.start)
    nulls_impl = impl_null_results(ctx.panel, ctx.sleeves, "xsmom",
                                   costs=COSTS, start=impl.start,
                                   paths=PATHS, seed=SEED)
    assert nulls_rec == nulls_impl


def test_materialized_values_equal_production_ranking_values(pg_session):
    """Store -> panel: every stored feature value equals the production
    ranking value (view-close formation return) for every eligible name at
    every rebalance, exact floats. Store -> live generator: the stored
    values equal _formation_returns — the code that ranks the real paper
    book — at a signal session."""
    s = pg_session
    _seed(s)
    _assert_no_member_dividends(s)
    audit = PostgresAuditLog(s, CLOCK)
    rec = run_recipe(s, audit, SPEC, clock=CLOCK, paths=2, seed=SEED)

    ctx = load_impl_context(s, top_universe=0)
    panel, members = ctx.panel, ctx.members
    feature = get_rank_feature(SPEC.rank_feature)
    values = feature_panel(s, feature, sorted(members),
                           start=panel.dates[0], end=panel.dates[-1],
                           dataset_version=rec.dataset_version)

    start_i = panel.index_at(rec.start)
    checked = 0
    for t in month_end_indices(panel.dates, start_i, len(panel.dates)):
        view = PanelView(panel, t)
        for sym in pit_eligible(view, members):
            c_form = view.close(sym, t - LOOKBACK)
            c_skip = view.close(sym, t - SKIP)
            assert c_form is not None and c_skip is not None
            assert values[sym][panel.dates[t]] == c_skip / c_form - 1.0, \
                f"{sym}@{panel.dates[t]}: store diverges from the panel math"
            checked += 1
    assert checked >= 100                     # the pin actually bit

    # the LIVE generator's ranking values (production signal path): activate
    # the members so the ADR-0007 universe query sees them, then compare
    s.execute(text(
        "UPDATE market.instruments SET is_active = TRUE "
        "WHERE symbol = ANY(:syms)"), {"syms": list(MEMBERS)})
    sig_session = rec.counts[-1][0]           # the final rebalance session
    formation, _ = _formation_returns(s, sig_session)
    for sym in MEMBERS:
        assert formation[sym] == values[sym][sig_session], \
            f"{sym}: live generator diverges from the store"


def test_no_dividend_precondition_is_load_bearing(pg_session):
    """PROOF that _assert_no_member_dividends is what makes the byte-identity
    pin an identity, not decoration: give ONE member ONE dividend inside a
    formation window and the TR-panel ranking value diverges from the
    store's price-basis value at that rebalance. On real dividend-paying
    data the recipe therefore runs a DIFFERENT trial from the impl-variant
    runner — deliberately, on the production price basis (module docstring)
    — and neither side is 'wrong': do not fix the store toward TR closes."""
    s = pg_session
    _seed(s)
    _assert_no_member_dividends(s)
    iid = s.execute(text(
        "SELECT id FROM market.instruments WHERE symbol = 'ZIV5'"
    )).scalar_one()

    def tr_and_price(panel, t: int) -> tuple[float, float]:
        view = PanelView(panel, t)
        c_form = view.close("ZIV5", t - LOOKBACK)
        c_skip = view.close("ZIV5", t - SKIP)
        assert c_form is not None and c_skip is not None
        day = panel.dates[t]
        price = compute_momentum(s, "ZIV5", iid, [day])[day]
        return c_skip / c_form - 1.0, price

    panel = load_impl_context(s, top_universe=0).panel
    ex = date(2012, 6, 15)                   # a fixture US session with a bar
    # a rebalance whose formation window (t-LOOKBACK, t-SKIP] spans the
    # ex-date: exactly where the TR reinvestment factor moves c_skip but
    # not c_form
    t = next(i for i in month_end_indices(panel.dates, LOOKBACK + 1,
                                          len(panel.dates))
             if panel.dates[i - LOOKBACK] < ex <= panel.dates[i - SKIP])
    tr_val, price_val = tr_and_price(panel, t)
    assert tr_val == price_val               # no dividends: exact identity

    s.execute(text(
        "INSERT INTO market.corporate_actions (instrument_id, action_date, "
        "action_type, amount, currency, source) "
        "VALUES (:iid, :d, 'dividend', '2.50', 'USD', 'test')"),
        {"iid": iid, "d": ex})
    panel2 = load_impl_context(s, top_universe=0).panel
    tr_val2, price_val2 = tr_and_price(panel2, t)
    assert price_val2 == price_val           # the store's price basis: unmoved
    assert tr_val2 != price_val2             # the TR ranking basis: moved


# ------------------------------------ registration-before-run DURABILITY ---

def test_mid_gauntlet_crash_registration_survives_run_rollback(pg_session,
                                                               monkeypatch):
    """Count honesty is DURABILITY, not intent: the registration commits in
    its OWN transaction before any gauntlet math, so a mid-gauntlet crash
    followed by the run transaction's ROLLBACK — exactly what the CLI's
    session_scope does on any exception — still leaves the trial registered.
    Verified from a SECOND independent session: the run session's own view
    proves nothing (the old version of this test queried its own uncommitted
    INSERT and pinned exactly the boundary that did not survive)."""
    s = pg_session
    _seed(s)
    audit = PostgresAuditLog(s, CLOCK)

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("monkey-null infrastructure died mid-gauntlet")

    monkeypatch.setattr(recipe_run_mod, "recipe_null_results", boom)
    base = _committed_momentum_count()
    with pytest.raises(RuntimeError, match="died mid-gauntlet"):
        run_recipe(s, audit, SPEC, clock=CLOCK, paths=4, seed=SEED)
    s.rollback()          # the session_scope crash path, simulated exactly

    engine = create_engine(URL)
    try:
        with engine.connect() as c:
            row = c.execute(text(
                "SELECT lineage, hypothesis, dataset_version, metrics, "
                "spec_hash FROM quant.trial_registry "
                "WHERE strategy_family = :f"), {"f": SPEC.family()}).one()
            assert row.lineage == "momentum"
            assert row.hypothesis == SPEC.rationale
            assert row.dataset_version is not None
            assert row.metrics == {}          # the honest pre-run stub
            assert row.spec_hash == SPEC.spec_hash()   # traceable to the spec
    finally:
        engine.dispose()
    # the crashed attempt still counts (ADR-0002 #1): one durable row landed
    assert _committed_momentum_count() == base + 1


def test_kill_leg_crash_cannot_erase_the_completed_main_trial(pg_session,
                                                              monkeypatch):
    """The main leg's registration AND metrics commit before the kill leg
    starts: a kill-leg crash + run-transaction rollback leaves the main
    trial fully enriched and the kill trial as a durable pre-run stub —
    a completed backtest can never be un-counted by a later leg's death."""
    s = pg_session
    _seed(s)
    audit = PostgresAuditLog(s, CLOCK)

    real = recipe_run_mod.recipe_null_results
    calls = {"n": 0}

    def kill_leg_boom(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] > 1:                    # main leg runs; kill leg dies
            raise RuntimeError("kill-leg null model died")
        return real(*args, **kwargs)

    monkeypatch.setattr(recipe_run_mod, "recipe_null_results", kill_leg_boom)
    base = _committed_momentum_count()
    with pytest.raises(RuntimeError, match="kill-leg null model died"):
        run_recipe_gauntlet(s, audit, SPEC, clock=CLOCK, paths=4, seed=SEED)
    s.rollback()          # the session_scope crash path, simulated exactly

    engine = create_engine(URL)
    try:
        with engine.connect() as c:
            main_row = c.execute(text(
                "SELECT metrics, spec_hash FROM quant.trial_registry "
                "WHERE strategy_family = :f"), {"f": SPEC.family()}).one()
            assert "total_return" in main_row.metrics   # enriched, durable
            assert main_row.spec_hash == SPEC.spec_hash()
            kill_row = c.execute(text(
                "SELECT metrics FROM quant.trial_registry "
                "WHERE strategy_family = :f"),
                {"f": SPEC.kill_family()}).one()
            assert kill_row.metrics == {}     # registered, never finished
    finally:
        engine.dispose()
    # both attempts count durably (main enriched + kill stub)
    assert _committed_momentum_count() == base + 2


def test_ad_hoc_windows_refused(pg_session):
    audit = PostgresAuditLog(pg_session, CLOCK)
    with pytest.raises(ValueError, match="ad-hoc windows"):
        run_recipe(pg_session, audit, SPEC, clock=CLOCK, paths=2, seed=SEED,
                   window_start=date(2013, 6, 3))


# ---------------------------------- the pre-committed pair + the report ---

def test_gauntlet_registers_both_trials_and_renders_the_report(pg_session):
    s = pg_session
    _seed(s)
    audit = PostgresAuditLog(s, CLOCK)
    main, kill = run_recipe_gauntlet(s, audit, SPEC, clock=CLOCK, paths=6,
                                     seed=SEED)

    assert main.family == "recipe-mom-12-1-top5"
    assert kill.family == "recipe-mom-12-1-top5-2013"
    assert trial_count(s, main.family) == 1
    assert trial_count(s, kill.family) == 1
    assert main.n_trials == 1
    assert kill.n_trials == 2      # the kill gate deflates at the FULL line
    assert kill.start >= SPEC.kill_start
    assert main.trials_before_total + 2 == kill.trials_after_total

    # metrics enriched IN PLACE on the pre-registered rows after the run;
    # each row's spec_hash column carries the EXACT hash the report, the
    # console line and the audit event print — the registry row is traceable
    # to the spec it claims to register (never an opaque re-digest)
    for r in (main, kill):
        row = s.execute(text(
            "SELECT metrics, hypothesis, dataset_version, spec_hash "
            "FROM quant.trial_registry WHERE strategy_family = :f"),
            {"f": r.family}).one()
        assert row.metrics["total_return"] == r.run.result.total_return
        assert row.metrics["n_rebalances"] == float(r.run.result.n_rebalances)
        assert row.hypothesis == SPEC.rationale
        assert row.dataset_version == r.dataset_version
        assert row.spec_hash == SPEC.spec_hash()

    ev = s.execute(text(
        "SELECT count(*) FROM audit.decision_events "
        "WHERE event_type = 'quant.backtest.completed' "
        "AND actor_id = 'recipe_run'")).scalar()
    assert ev == 2

    report = render_recipe_report(main, kill, paths=6)
    for r in (main, kill):
        verdict = "PASS" if r.gate.passed else "FAIL"
        assert f"### Gate verdict: **{verdict}**" in report   # verbatim
        for reason in r.gate.reasons:
            assert reason in report                           # verbatim
        assert r.trial_id in report
    assert (f"lineage '{SPEC.lineage}', {main.gate.n_trials} "
            "registered trials") in report                    # count basis
    assert SPEC.spec_hash() in report
    assert main.dataset_version in report
    assert "demote-only" in report
    assert "None sought here" in report
    assert SPEC.rationale in report                           # the hypothesis
