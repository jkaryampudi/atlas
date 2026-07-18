import pytest
from sqlalchemy import text

from atlas.dcp.backtest.registry import lineage_count, register_trial, trial_count
from tests.conftest import requires_pg

pytestmark = requires_pg


def test_every_backtest_is_registered(pg_session):
    s = pg_session
    s.execute(text("TRUNCATE quant.trial_registry"))
    s.commit()
    for i in range(3):
        register_trial(s, family="momentum", lineage="momentum",
                       spec={"v": i}, metrics={"sharpe": 1.0 + i})
    s.commit()
    assert trial_count(s, "momentum") == 3
    assert trial_count(s, "nonexistent") == 0


def test_register_trial_requires_lineage(pg_session):
    """ADR-0016: every NEW registration names its research line — omitting the
    kwarg is a TypeError, and blank strings are refused loudly."""
    s = pg_session
    with pytest.raises(TypeError):
        register_trial(s, family="momentum", spec={"v": 1}, metrics={})
    with pytest.raises(ValueError, match="lineage is required"):
        register_trial(s, family="momentum", lineage="  ",
                       spec={"v": 1}, metrics={})


def test_register_trial_persists_lineage(pg_session):
    s = pg_session
    s.execute(text("TRUNCATE quant.trial_registry"))
    s.commit()
    rid = register_trial(s, family="xsmom-impl500-tr", lineage="momentum",
                         spec={"v": 1}, metrics={})
    assert s.execute(text(
        "SELECT lineage FROM quant.trial_registry WHERE id = :r"),
        {"r": rid}).scalar() == "momentum"


def test_lineage_count_spans_family_names(pg_session):
    """The counting defect (ADR-0016): a freshly-named variant must NOT reset
    the deflated-Sharpe penalty. The lineage count spans every family name
    the research line has ever worn; per-family counts stay per-family."""
    s = pg_session
    s.execute(text("TRUNCATE quant.trial_registry"))
    s.commit()
    for fam in ("momentum", "xsmom", "xsmom-pit-tr", "xsmom-impl500-tr"):
        register_trial(s, family=fam, lineage="momentum",
                       spec={"fam": fam}, metrics={})
    register_trial(s, family="pead-sue-tr", lineage="pead",
                   spec={"fam": "pead-sue-tr"}, metrics={})
    s.commit()
    assert trial_count(s, "xsmom-impl500-tr") == 1      # first-in-family ...
    assert lineage_count(s, "momentum") == 4            # ... but not first-in-line
    assert lineage_count(s, "pead") == 1
    assert lineage_count(s, "nonexistent") == 0
