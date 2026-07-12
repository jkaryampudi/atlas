"""fxlab gauntlet (ADR-0008 §5): null-model determinism and matched
exposure/turnover, thresholds anchored to the equity gate (never restated),
the walk-forward majority rule, and benchmark-zero verdict wording. DB-free:
trial registration and audit are exercised in the integration suite."""
import inspect
import random
from collections import Counter
from datetime import date, timedelta

from atlas.dcp.backtest.real_run import EMBARGO, HORIZON, K_FOLDS
from atlas.dcp.backtest.validation import null_model_gate
from atlas.fxlab.candidates import WARMUP
from atlas.fxlab.engine import FxBar, run_fx_backtest
from atlas.fxlab.gauntlet import (DSR_MIN, P_MAX, FxVerdict, block_path,
                                  evaluate_candidate, fx_walk_forward,
                                  null_total_returns, position_segments,
                                  wf_majority_ok)

D0 = date(2024, 1, 1)


def fb(o: float, h: float, lo: float, c: float, day: int) -> FxBar:
    return FxBar(bar_date=D0 + timedelta(days=day), open=o, high=h, low=lo, close=c)


def _walk(n: int, seed: int = 5) -> list[FxBar]:
    rng = random.Random(seed)
    out, px = [], 1.10
    for d in range(n):
        o = px * (1 + rng.uniform(-0.002, 0.002))
        c = o * (1 + rng.uniform(-0.006, 0.006))
        out.append(fb(o, max(o, c) * 1.001, min(o, c) * 0.999, c, d))
        px = c
    return out


def test_thresholds_are_the_equity_gates_never_restated():
    """P_MAX/DSR_MIN must track dcp/backtest/validation.py's own defaults —
    if the house gate tightens, fxlab tightens with it; any hardcoded copy
    in gauntlet.py fails here."""
    params = inspect.signature(null_model_gate).parameters
    assert P_MAX == params["p_max"].default
    assert DSR_MIN == params["dsr_min"].default


def test_position_segments_run_length():
    assert position_segments([1, 1, 0, -1, -1, -1]) == [(1, 2), (0, 1), (-1, 3)]
    assert position_segments([]) == []


def test_block_path_matches_exposure_and_bounds_turnover():
    positions = [0, 0, 1, 1, 1, 0, -1, -1, 1, 0, 0, 0, -1]
    segs = position_segments(positions)
    changes = sum(1 for a, b in zip(positions, positions[1:]) if a != b)
    for seed in range(20):
        path = block_path(segs, random.Random(seed))
        assert Counter(path) == Counter(positions)          # exposure matched exactly
        path_changes = sum(1 for a, b in zip(path, path[1:]) if a != b)
        assert path_changes <= changes                       # turnover from above


def test_null_total_returns_deterministic_by_seed():
    bars = _walk(80)
    positions = [1] * 20 + [0] * 19 + [-1] * 20
    a = null_total_returns(bars, positions, 20, paths=50, seed=7)
    b = null_total_returns(bars, positions, 20, paths=50, seed=7)
    c = null_total_returns(bars, positions, 20, paths=50, seed=8)
    assert a == b
    assert a != c


def test_wf_majority_rule_matches_approval_gate():
    """approval.py: positive_folds >= len(folds)//2 + 1."""
    assert not wf_majority_ok(2, 4)
    assert wf_majority_ok(3, 4)
    assert not wf_majority_ok(1, 3)
    assert wf_majority_ok(2, 3)


def test_fx_walk_forward_uses_real_run_constants():
    bars = _walk(WARMUP + 400)
    wf = fx_walk_forward(bars, lambda h: 1 if h[-1].close > h[-2].close else -1,
                         warmup=WARMUP)
    assert len(wf.fold_results) == K_FOLDS
    assert (K_FOLDS, HORIZON, EMBARGO) == (4, 40, 10)  # pin: imported policy moved


def test_verdict_benchmark_is_zero_and_reasons_verbatim():
    """A churning strategy on flat prices loses exactly its costs; every
    gate reason must be present and the ADR-0008 benchmark-zero wording
    explicit. Null paths of identical blocks on flat prices tie the
    strategy -> p = 1.0."""
    bars = [fb(1.0, 1.0, 1.0, 1.0, d) for d in range(WARMUP + 60)]
    result = run_fx_backtest(bars, lambda h: 1 if len(h) % 2 else -1,
                             start_i=WARMUP, end_i=len(bars))
    wf = fx_walk_forward(bars, lambda h: 1 if len(h) % 2 else -1, warmup=WARMUP)
    v = evaluate_candidate(name="churn", bars=bars, result=result, n_trials=3,
                           trial_id="t-0", wf=wf, paths=25, seed=7)
    assert isinstance(v, FxVerdict)
    assert not v.passed
    assert v.result.total_return < 0
    assert v.null_p == 1.0
    assert any("ADR-0008 §5: the benchmark is zero" in r for r in v.reasons)
    assert any(r.startswith("null-model: p=") for r in v.reasons)
    assert any("deflated Sharpe" in r for r in v.reasons)
    assert any("walk-forward" in r for r in v.reasons)


def test_no_profit_target_anywhere_in_the_sandbox():
    """ADR-0008 §7 as a grep-proof: no fxlab source mentions a profit target
    or the refused A$50/day quota as an input."""
    from pathlib import Path
    src = Path(__file__).parents[2] / "atlas" / "fxlab"
    for py in src.rglob("*.py"):
        text = py.read_text().lower()
        assert "profit_target" not in text, py
        assert "target_profit" not in text, py
