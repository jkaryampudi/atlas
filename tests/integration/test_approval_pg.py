import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).parents[1]))
from fixtures.synthetic import regime_series  # noqa: E402

from atlas.dcp.backtest.approval import evaluate_approval, record_and_transition  # noqa: E402
from atlas.dcp.backtest.registry import register_trial  # noqa: E402
from atlas.dcp.backtest.validation import GateReport  # noqa: E402
from atlas.dcp.backtest.walkforward import walk_forward  # noqa: E402
from atlas.dcp.signals.momentum.v1 import SPEC, momentum_v1  # noqa: E402
from tests.conftest import requires_pg  # noqa: E402

pytestmark = requires_pg


def _clean(s):
    s.execute(text("TRUNCATE quant.validation_reports, quant.trial_registry, "
                   "quant.strategies RESTART IDENTITY CASCADE"))
    s.commit()


def test_refusal_without_artifacts(pg_session):
    s = pg_session
    _clean(s)
    d = evaluate_approval(s, family="momentum", gate=None, wf=None,
                          oos_untouched_attested=False)
    assert not d.approved and len(d.reasons) >= 3


def test_refusal_on_trial_count_mismatch(pg_session):
    s = pg_session
    _clean(s)
    for i in range(5):
        register_trial(s, family="momentum", spec={"i": i}, metrics={})
    s.commit()
    gate = GateReport(strategy_return=1.0, bh_return=0.1, null_p_value=0.0,
                      dsr=0.99, n_trials=1, passed=True, reasons=[])
    d = evaluate_approval(s, family="momentum", gate=gate, wf=None,
                          oos_untouched_attested=True)
    assert not d.approved
    assert any("true count" in r for r in d.reasons)


def test_full_package_approves_and_transitions(pg_session):
    s = pg_session
    _clean(s)
    sid = s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, state) "
        "VALUES ('momentum','trend_rs_vol','1.0.0','{}','backtested') RETURNING id"
    )).scalar_one()
    register_trial(s, family="momentum", spec=SPEC, metrics={"sharpe": 1.85})
    wf = walk_forward(regime_series(), lambda b, t: momentum_v1,
                      k=4, horizon=40, embargo=10, warmup=60)
    gate = GateReport(strategy_return=1.02, bh_return=0.12, null_p_value=0.0,
                      dsr=1.0, n_trials=1, passed=True, reasons=[])
    d = evaluate_approval(s, family="momentum", gate=gate, wf=wf,
                          oos_untouched_attested=True)
    assert d.approved
    record_and_transition(s, strategy_id=str(sid), backtest_id=None, decision=d,
                          checklist={"gate": "pass", "wf_positive": wf.positive_folds})
    s.commit()
    assert s.execute(text("SELECT state FROM quant.strategies WHERE id=:i"),
                     {"i": sid}).scalar() == "validated"
    assert s.execute(text("SELECT verdict FROM quant.validation_reports")).scalar() == "approve"
