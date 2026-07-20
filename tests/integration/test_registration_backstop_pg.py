"""The double-burn backstop (chassis-review residual, closed): a duplicate
recipe family is refused at the REGISTRATION CHOKEPOINT itself — under a
transaction-scoped advisory lock, across processes — unless the repeat is
explicit (rerun=True / --rerun). The exact race (a second registration
arriving while the first holds the lock) is simulated with a real second
connection holding the lock; the loser must wait, then see the winner's
committed row, then refuse. Committed registry rows are scrubbed by family.
"""
from __future__ import annotations

import threading
import time
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.factory.recipe_run import DuplicateFamilyError, run_recipe
from atlas.dcp.factory.spec import RecipeSpec
from tests.conftest import URL, requires_pg
from tests.integration.test_impl_variant_pg import _seed as _seed_ziv_world

pytestmark = requires_pg

_CLOCK = FrozenClock(datetime(2026, 7, 20, 6, 0, tzinfo=UTC))
_FAMILY = "recipe-backstop-check"


def _scrub() -> None:
    engine = create_engine(URL)
    try:
        with engine.begin() as c:
            c.execute(text("DELETE FROM quant.trial_registry "
                           "WHERE strategy_family LIKE 'recipe-backstop-%'"))
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _registry_isolation(pg_session):
    _scrub()
    yield
    _scrub()


def _spec() -> RecipeSpec:
    return RecipeSpec(
        name="backstop-check", rank_feature="momentum_12_1", direction="desc",
        top_n=5, rebalance="monthly", universe="pit-sp500", lineage="momentum",
        rationale="Backstop witness: winners keep winning (Jegadeesh-Titman "
                  "1993); duplicate-registration refusal under the lock.",
        kill_start=date(2013, 1, 2))


def test_duplicate_family_refused_at_chokepoint_and_rerun_is_explicit(pg_session):
    s = pg_session
    _seed_ziv_world(s)
    audit = PostgresAuditLog(s, _CLOCK)
    run_recipe(s, audit, _spec(), clock=_CLOCK, paths=2, seed=7)
    count = s.execute(text("SELECT count(*) FROM quant.trial_registry "
                           "WHERE strategy_family = :f"), {"f": _FAMILY}).scalar()
    assert count == 1

    # the accidental repeat: refused at the chokepoint, registers NOTHING
    with pytest.raises(DuplicateFamilyError, match="one name, one experiment"):
        run_recipe(s, audit, _spec(), clock=_CLOCK, paths=2, seed=7)
    assert s.execute(text("SELECT count(*) FROM quant.trial_registry "
                          "WHERE strategy_family = :f"),
                     {"f": _FAMILY}).scalar() == 1

    # the DELIBERATE repeat: rerun=True registers honestly as a new burn
    run_recipe(s, audit, _spec(), clock=_CLOCK, paths=2, seed=7, rerun=True)
    assert s.execute(text("SELECT count(*) FROM quant.trial_registry "
                          "WHERE strategy_family = :f"),
                     {"f": _FAMILY}).scalar() == 2


def test_gauntlet_early_check_and_rerun_plumbing(pg_session):
    """The gauntlet refuses a duplicate pair BEFORE any leg runs (no half-run
    state, nothing burned), and rerun=True forwards to BOTH legs — each family
    gains exactly one new counted row on the explicit repeat. The CLI --rerun
    flag is pinned on the extracted parser."""
    from atlas.dcp.factory.recipe_run import (
        _build_parser,
        run_recipe_gauntlet,
    )

    s = pg_session
    _seed_ziv_world(s)
    audit = PostgresAuditLog(s, _CLOCK)

    def counts() -> tuple[int, int]:
        main = s.execute(text("SELECT count(*) FROM quant.trial_registry "
                              "WHERE strategy_family = :f"),
                         {"f": _FAMILY}).scalar()
        kill = s.execute(text("SELECT count(*) FROM quant.trial_registry "
                              "WHERE strategy_family = :f"),
                         {"f": f"{_FAMILY}-2013"}).scalar()
        return int(main), int(kill)

    run_recipe_gauntlet(s, audit, _spec(), clock=_CLOCK, paths=2, seed=7)
    assert counts() == (1, 1)
    # the accidental repeat refuses EARLY: neither leg runs, nothing burns
    with pytest.raises(DuplicateFamilyError, match="refused before any leg"):
        run_recipe_gauntlet(s, audit, _spec(), clock=_CLOCK, paths=2, seed=7)
    assert counts() == (1, 1)
    # the explicit repeat burns BOTH legs again — rerun forwards to each
    run_recipe_gauntlet(s, audit, _spec(), clock=_CLOCK, paths=2, seed=7,
                        rerun=True)
    assert counts() == (2, 2)
    # CLI plumbing: --rerun parses True, defaults False
    parser = _build_parser()
    ns = parser.parse_args(["--spec", "x.json", "--rerun"])
    assert ns.rerun is True
    assert parser.parse_args(["--spec", "x.json"]).rerun is False


def test_race_loser_waits_on_the_lock_then_refuses(pg_session):
    """The EXACT chassis-review race: surface A is mid-registration (advisory
    lock held, row not yet committed) when surface B's registration arrives.
    B must BLOCK on the lock — not slip past a stale count — and, once A
    commits, must see A's row and refuse. Uses a real second connection so the
    lock is genuinely cross-connection, as it would be cross-process."""
    s = pg_session
    _seed_ziv_world(s)
    audit = PostgresAuditLog(s, _CLOCK)

    engine = create_engine(URL)
    winner = engine.connect()
    try:
        wtx = winner.begin()
        # surface A: takes the chokepoint lock and registers, NOT yet committed
        winner.execute(text(
            "SELECT pg_advisory_xact_lock(hashtextextended(:f, 0))"),
            {"f": _FAMILY})
        winner.execute(text(
            "INSERT INTO quant.trial_registry (strategy_family, spec_hash, "
            " metrics, lineage) VALUES (:f, 'racewinner', '{}'::jsonb, "
            " 'momentum')"), {"f": _FAMILY})

        outcome: dict[str, object] = {}

        def surface_b() -> None:
            try:
                run_recipe(s, audit, _spec(), clock=_CLOCK, paths=2, seed=7)
                outcome["result"] = "ran"
            except DuplicateFamilyError:
                outcome["result"] = "refused"
            except Exception as e:  # noqa: BLE001 — surface the real failure
                outcome["result"] = f"error: {e}"

        t = threading.Thread(target=surface_b, daemon=True)
        t.start()
        # DETERMINISTIC witness (review 2026-07: a sleep-assert is vacuous on
        # a slow machine): poll pg_locks until B's session is visibly WAITING
        # on an advisory lock. If the chokepoint lock were removed from
        # run_recipe, no waiting row could ever appear and this times out —
        # the test detects the lock's absence, not merely B's slowness.
        observer = engine.connect()
        try:
            waited = False
            for _ in range(240):           # up to 120s for panel+materialize
                n_waiting = observer.execute(text(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE locktype = 'advisory' AND NOT granted")).scalar()
                if n_waiting:
                    waited = True
                    break
                if "result" in outcome:    # B finished without ever waiting
                    break
                time.sleep(0.5)
        finally:
            observer.close()
        assert waited, (
            f"surface B never blocked on the advisory lock "
            f"(outcome={outcome.get('result')!r}) — the chokepoint lock is "
            f"missing or bypassed")
        assert "result" not in outcome     # still blocked while A holds it
        wtx.commit()                       # A's registration becomes durable
        t.join(timeout=120)
        assert outcome.get("result") == "refused"
        # exactly ONE row: the winner's — the loser burned nothing
        n = s.execute(text("SELECT count(*) FROM quant.trial_registry "
                           "WHERE strategy_family = :f"), {"f": _FAMILY}).scalar()
        assert n == 1
    finally:
        winner.close()
        engine.dispose()
