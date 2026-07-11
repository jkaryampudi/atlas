from sqlalchemy import text

from atlas.dcp.backtest.registry import register_trial, trial_count
from tests.conftest import requires_pg

pytestmark = requires_pg


def test_every_backtest_is_registered(pg_session):
    s = pg_session
    s.execute(text("TRUNCATE quant.trial_registry"))
    s.commit()
    for i in range(3):
        register_trial(s, family="momentum",
                       spec={"v": i}, metrics={"sharpe": 1.0 + i})
    s.commit()
    assert trial_count(s, "momentum") == 3
    assert trial_count(s, "nonexistent") == 0
