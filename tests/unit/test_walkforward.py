import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from fixtures.synthetic import regime_series  # noqa: E402

from atlas.dcp.backtest.walkforward import (leakage_free, purged_folds,  # noqa: E402
                                            walk_forward)
from atlas.dcp.signals.momentum.v1 import momentum_v1  # noqa: E402


def test_every_fold_is_leakage_free():
    for k in (3, 4, 5):
        for horizon in (10, 40):
            for embargo in (0, 10):
                folds = purged_folds(1200, k=k, horizon=horizon, embargo=embargo,
                                     warmup=60)
                assert len(folds) == k
                for f in folds:
                    assert leakage_free(f, horizon=horizon, embargo=embargo)
                    # explicit: the day just before test start is purged
                    assert f.test_start - 1 not in f.train_days


def test_folds_cover_span_without_overlap():
    folds = purged_folds(1200, k=4, horizon=40, embargo=10, warmup=60)
    edges = [(f.test_start, f.test_end) for f in folds]
    assert edges[0][0] == 60 and edges[-1][1] == 1200
    for (a1, b1), (a2, b2) in zip(edges, edges[1:]):
        assert b1 == a2


def test_walk_forward_momentum_on_regime_fixture():
    wf = walk_forward(regime_series(), lambda b, t: momentum_v1,
                      k=4, horizon=40, embargo=10, warmup=60)
    assert wf.positive_folds == 3
    assert wf.mean_return > 0.15
    assert wf.worst_fold_return > -0.05
