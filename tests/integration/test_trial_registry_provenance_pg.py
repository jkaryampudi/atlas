"""register_trial provenance (ADR-0011 step 1, roadmap 0.1 gap-fill):
provenance kwargs default None (columns land NULL — honest history), and the
new kwargs persist so a future backtest CAN pin its hypothesis and
feature-store dataset vintage. (lineage became REQUIRED with ADR-0016 —
provenance defaults are unchanged around it.)"""
from __future__ import annotations

from sqlalchemy import text

from atlas.dcp.backtest.registry import register_trial, trial_count
from tests.conftest import requires_pg

pytestmark = requires_pg


def test_existing_call_shape_unchanged_null_provenance(pg_session):
    s = pg_session
    rid = register_trial(s, family="prov-test", lineage="prov-test",
                         spec={"v": 1}, metrics={"sharpe": 0.1})
    row = s.execute(text(
        "SELECT strategy_family, hypothesis, dataset_version "
        "FROM quant.trial_registry WHERE id = :r"), {"r": rid}).one()
    assert row.strategy_family == "prov-test"
    assert row.hypothesis is None and row.dataset_version is None
    assert trial_count(s, "prov-test") >= 1     # counted exactly as before


def test_new_kwargs_are_persisted(pg_session):
    s = pg_session
    rid = register_trial(
        s, family="prov-test", lineage="prov-test",
        spec={"v": 2}, metrics={"sharpe": 0.2},
        hypothesis="PEAD drift persists 63 sessions in US large caps",
        dataset_version="a" * 64)
    row = s.execute(text(
        "SELECT hypothesis, dataset_version FROM quant.trial_registry "
        "WHERE id = :r"), {"r": rid}).one()
    assert row.hypothesis == ("PEAD drift persists 63 sessions in US "
                              "large caps")
    assert row.dataset_version == "a" * 64
