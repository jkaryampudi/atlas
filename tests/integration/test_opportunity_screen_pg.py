"""Opportunity screen (opportunity_screen.screen_opportunities): ranks the active
US universe by the health composite, then enriches the top-N with Atlas's
valuation verdict + fragility. The synthetic universe (reused from the health
test) is arranged so each name sits at the SAME percentile on every factor, so
the composite ranking is exact: PONE(100) > SUBJ(75) > PTWO(50) > PTHR(25).

MEASURED-NEVER-APPLIED: the screen is a research candidate board; these tests
assert the ranking and shape, not any capital action (there is none).
"""
from __future__ import annotations

from sqlalchemy import text

from atlas.dcp.research.opportunity_screen import (
    SCREEN_SOURCE,
    screen_opportunities,
    snapshot_board_picks,
)
from tests.conftest import requires_pg
from tests.integration.test_health_score_pg import (
    _AS_OF,
    _NAMES,
    _fund,
    _instrument,
    _payload,
    _seed_momentum,
    _seed_universe,
)

pytestmark = requires_pg


def _seed(s):
    """Seed the health universe, then repair OHLC. The health fixture updates
    only each last bar's close (health reads close directly), leaving high/low
    stale; compute_models validates OHLC on construction, so make the seeded
    bars consistent (real ingested bars already are)."""
    ids = _seed_universe(s)
    s.execute(text(
        "UPDATE market.price_bars_daily "
        "SET high = GREATEST(high, close), low = LEAST(low, close), "
        "    volume = GREATEST(volume, 1)"))
    return ids


def test_ranked_board_orders_by_health_composite(pg_session):
    s = pg_session
    _seed(s)
    out = screen_opportunities(s, _AS_OF, top_n=4)

    assert out["universe_n"] == 4 and out["ranked_n"] == 4
    board = out["board"]
    assert [r["symbol"] for r in board] == ["PONE", "SUBJ", "PTWO", "PTHR"]
    assert [r["rank"] for r in board] == [1, 2, 3, 4]
    # each name sits at the same percentile on every factor -> composite == that
    assert [r["health_composite"] for r in board] == [100.0, 75.0, 50.0, 25.0]
    # sector carried through from the instrument row
    assert all(r["sector"] == "TestTech" for r in board)
    # every enrichment key present (values may be None on this thin fixture, but
    # the screen must never omit a column or crash on a fail-soft valuation)
    for r in board:
        for k in ("pillars", "price", "valuation_verdict", "valuation_basis",
                  "upside_to_central_pct", "technical_trend", "fragility",
                  "fragility_alerts"):
            assert k in r, k
        assert set(r["pillars"]) == {"relative_value", "profitability", "growth",
                                     "cash_flow", "momentum"}


def test_top_n_truncates_the_board(pg_session):
    s = pg_session
    _seed(s)
    out = screen_opportunities(s, _AS_OF, top_n=2)
    # whole universe still ranked; only the board is truncated to the top 2
    assert out["ranked_n"] == 4 and out["universe_n"] == 4
    assert [r["symbol"] for r in out["board"]] == ["PONE", "SUBJ"]


def test_empty_universe_is_empty_board(pg_session):
    s = pg_session
    out = screen_opportunities(s, _AS_OF, top_n=10)
    assert out["universe_n"] == 0 and out["ranked_n"] == 0
    assert out["board"] == []


def test_ties_rank_deterministically_by_instrument_id(pg_session):
    # regression (adversarial review 2026-07): the board must sort on the
    # full-precision composite with a deterministic tiebreak, NOT the 0.1-rounded
    # display value in Postgres scan order. Seed a TWIN with PONE's exact factors
    # -> an exact composite tie -> the order must be fixed by instrument id and
    # identical across runs, never dependent on heap-scan order.
    s = pg_session
    ids = _seed(s)
    f, (prior, last) = _NAMES["PONE"]
    twin_id = _instrument(s, "PTWIN")
    _fund(s, twin_id, _payload(**f))
    _seed_momentum(s, twin_id, prior, last)
    s.execute(text("UPDATE market.price_bars_daily SET high = GREATEST(high, close), "
                   "low = LEAST(low, close), volume = GREATEST(volume, 1)"))

    out1 = screen_opportunities(s, _AS_OF, top_n=10)
    out2 = screen_opportunities(s, _AS_OF, top_n=10)
    order1 = [r["symbol"] for r in out1["board"]]
    assert order1 == [r["symbol"] for r in out2["board"]]        # reproducible

    # PONE and PTWIN tie exactly; their relative order is the instrument-id sort
    pos = {r["symbol"]: i for i, r in enumerate(out1["board"])}
    first = "PONE" if str(ids["PONE"]) < str(twin_id) else "PTWIN"
    second = "PTWIN" if first == "PONE" else "PONE"
    assert pos[first] < pos[second]
    # they carry the identical composite (the tie is real, not a rounding artifact)
    comps = {r["symbol"]: r["health_composite"] for r in out1["board"]}
    assert comps["PONE"] == comps["PTWIN"]


def test_snapshot_records_board_picks_and_is_idempotent(pg_session):
    # the board's top-K are recorded as MEASURED source-picks so the existing
    # grade/edge machinery can score the screen vs SPY. Records the right names,
    # tagged with SCREEN_SOURCE, and a re-run is a no-op (idempotent per date).
    s = pg_session
    _seed(s)
    res = snapshot_board_picks(s, _AS_OF, top_k=3)
    assert [o for _sym, o in res] == ["recorded", "recorded", "recorded"]
    assert {sym for sym, _o in res} == {"PONE", "SUBJ", "PTWO"}   # top 3 by composite
    n = s.execute(text("SELECT count(*) FROM research.source_picks WHERE source = :s"),
                  {"s": SCREEN_SOURCE}).scalar()
    assert n == 3
    # a second run for the same date records nothing new (safe monthly re-run)
    res2 = snapshot_board_picks(s, _AS_OF, top_k=3)
    assert [o for _sym, o in res2] == ["duplicate", "duplicate", "duplicate"]
    assert s.execute(text("SELECT count(*) FROM research.source_picks "
                          "WHERE source = :s"), {"s": SCREEN_SOURCE}).scalar() == 3
