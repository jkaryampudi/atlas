import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).parents[1]))
from fixtures.synthetic import regime_series  # noqa: E402

from atlas.core.audit_repo import PostgresAuditLog  # noqa: E402
from atlas.core.clock import FrozenClock  # noqa: E402
from atlas.dcp.backtest.approval import (  # noqa: E402
    evaluate_approval,
    record_and_transition,
    require_signed_validation_artifact,
    transition_to_paper,
)
from atlas.dcp.backtest.registry import register_trial  # noqa: E402
from atlas.dcp.backtest.validation import GateReport  # noqa: E402
from atlas.dcp.backtest.walkforward import walk_forward  # noqa: E402
from atlas.dcp.signals.momentum.v1 import SPEC, momentum_v1  # noqa: E402
from tests.conftest import requires_pg  # noqa: E402

pytestmark = requires_pg

CLOCK = FrozenClock(datetime(2026, 7, 13, 2, tzinfo=UTC))


def _clean(s):
    s.execute(text("TRUNCATE quant.validation_reports, quant.trial_registry, "
                   "quant.strategies RESTART IDENTITY CASCADE"))
    s.commit()


def test_refusal_without_artifacts(pg_session):
    s = pg_session
    _clean(s)
    d = evaluate_approval(s, family="momentum", lineage="momentum",
                          gate=None, wf=None, oos_untouched_attested=False)
    assert not d.approved and len(d.reasons) >= 3


def test_refusal_on_trial_count_mismatch(pg_session):
    s = pg_session
    _clean(s)
    for i in range(5):
        register_trial(s, family="momentum", lineage="momentum",
                       spec={"i": i}, metrics={})
    s.commit()
    gate = GateReport(strategy_return=1.0, bh_return=0.1, null_p_value=0.0,
                      dsr=0.99, n_trials=1, passed=True, reasons=[])
    d = evaluate_approval(s, family="momentum", lineage="momentum",
                          gate=gate, wf=None, oos_untouched_attested=True)
    assert not d.approved
    assert any("true lineage count" in r for r in d.reasons)


def test_full_package_approves_and_transitions(pg_session):
    s = pg_session
    _clean(s)
    sid = s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, state) "
        "VALUES ('momentum','trend_rs_vol','1.0.0','{}','backtested') RETURNING id"
    )).scalar_one()
    register_trial(s, family="momentum", lineage="momentum",
                   spec=SPEC, metrics={"sharpe": 1.85})
    wf = walk_forward(regime_series(), lambda b, t: momentum_v1,
                      k=4, horizon=40, embargo=10, warmup=60)
    gate = GateReport(strategy_return=1.02, bh_return=0.12, null_p_value=0.0,
                      dsr=1.0, n_trials=1, passed=True, reasons=[])
    d = evaluate_approval(s, family="momentum", lineage="momentum",
                          gate=gate, wf=wf, oos_untouched_attested=True)
    assert d.approved
    record_and_transition(s, strategy_id=str(sid), backtest_id=None, decision=d,
                          checklist={"gate": "pass", "wf_positive": wf.positive_folds})
    s.commit()
    assert s.execute(text("SELECT state FROM quant.strategies WHERE id=:i"),
                     {"i": sid}).scalar() == "validated"
    assert s.execute(text("SELECT verdict FROM quant.validation_reports")).scalar() == "approve"


# ---------------------------------------------------------------------------
# transition_to_paper — the Principal's signature (validated -> paper)
# ---------------------------------------------------------------------------

def _validated_strategy(s) -> str:
    """A strategy that has legitimately reached 'validated' with an approve
    report, via the same artifact path as the test above."""
    sid = s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, state) "
        "VALUES ('momentum','trend_rs_vol','1.0.0','{}','backtested') RETURNING id"
    )).scalar_one()
    register_trial(s, family="momentum", lineage="momentum",
                   spec=SPEC, metrics={"sharpe": 1.85})
    wf = walk_forward(regime_series(), lambda b, t: momentum_v1,
                      k=4, horizon=40, embargo=10, warmup=60)
    gate = GateReport(strategy_return=1.02, bh_return=0.12, null_p_value=0.0,
                      dsr=1.0, n_trials=1, passed=True, reasons=[])
    d = evaluate_approval(s, family="momentum", lineage="momentum",
                          gate=gate, wf=wf, oos_untouched_attested=True)
    record_and_transition(s, strategy_id=str(sid), backtest_id=None, decision=d,
                          checklist={})
    return str(sid)


def test_paper_transition_requires_validated_state(pg_session):
    s = pg_session
    _clean(s)
    sid = s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, state) "
        "VALUES ('momentum','trend_rs_vol','1.0.0','{}','backtested') RETURNING id"
    )).scalar_one()
    audit = PostgresAuditLog(s, CLOCK)
    with pytest.raises(ValueError, match="not 'validated'"):
        transition_to_paper(s, audit, strategy_id=str(sid),
                            approved_by="test principal",
                            decision_ref="ADR-test", clock=CLOCK)


def test_paper_transition_requires_approve_report(pg_session):
    s = pg_session
    _clean(s)
    sid = s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, state) "
        "VALUES ('momentum','trend_rs_vol','1.0.0','{}','validated') RETURNING id"
    )).scalar_one()  # state forged by hand: no validation report exists
    audit = PostgresAuditLog(s, CLOCK)
    with pytest.raises(ValueError, match="signed validation artifact"):
        transition_to_paper(s, audit, strategy_id=str(sid),
                            approved_by="test principal",
                            decision_ref="ADR-test", clock=CLOCK)


def test_paper_transition_records_approver_and_audit_event(pg_session):
    s = pg_session
    _clean(s)
    sid = _validated_strategy(s)
    audit = PostgresAuditLog(s, CLOCK)
    transition_to_paper(s, audit, strategy_id=sid,
                        approved_by="Jay Karyampudi (Principal)",
                        decision_ref="ADR-0010", clock=CLOCK)
    s.commit()
    row = s.execute(text(
        "SELECT state, approved_by, approved_at FROM quant.strategies "
        "WHERE id=:i"), {"i": sid}).mappings().one()
    assert row["state"] == "paper"
    assert row["approved_by"] == "Jay Karyampudi (Principal)"
    assert row["approved_at"] == CLOCK.now()
    ev = s.execute(text(
        "SELECT actor_type, actor_id, payload FROM audit.decision_events "
        "WHERE event_type='quant.strategy.approved_paper' AND entity_id=:i"),
        {"i": sid}).mappings().one()
    assert ev["actor_type"] == "human"
    assert ev["payload"]["decision_ref"] == "ADR-0010"
    assert ev["payload"]["new_state"] == "paper"


def test_paper_transition_refuses_rejected_strategy(pg_session):
    s = pg_session
    _clean(s)
    sid = s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, state) "
        "VALUES ('momentum','trend_rs_vol','1.0.0','{}','validated') RETURNING id"
    )).scalar_one()
    s.execute(text(
        "INSERT INTO quant.validation_reports "
        "(strategy_id, backtest_id, checklist, verdict, reasons) "
        "VALUES (:sid, NULL, '{}', 'reject', 'gate failed')"), {"sid": sid})
    audit = PostgresAuditLog(s, CLOCK)
    with pytest.raises(ValueError, match="refusing paper transition"):
        transition_to_paper(s, audit, strategy_id=str(sid),
                            approved_by="test principal",
                            decision_ref="ADR-test", clock=CLOCK)


# ---------------------------------------------------------------------------
# P0 (ADR-0018) fail-closed promotion gate — objectives 7b / 7c + freshness
# ---------------------------------------------------------------------------

def test_p0_missing_validation_artifact_fails_closed(pg_session):
    """Objective 7b: with no validation report at all, the signed-artifact gate
    refuses — promotion cannot proceed on a missing artifact."""
    s = pg_session
    _clean(s)
    sid = s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, state) "
        "VALUES ('momentum','trend_rs_vol','1.0.0','{}','validated') RETURNING id"
    )).scalar_one()
    with pytest.raises(ValueError, match="signed validation artifact"):
        require_signed_validation_artifact(s, str(sid))


def test_p0_failed_gate_cannot_be_overridden_by_convenience_flag(pg_session):
    """Objective 7c: a strategy whose latest gate is a 'reject' cannot be
    promoted, AND there is no convenience/override parameter that bypasses the
    artifact requirement (an unexpected kwarg raises TypeError — no such flag
    exists in the API)."""
    s = pg_session
    _clean(s)
    sid = s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, state) "
        "VALUES ('momentum','trend_rs_vol','1.0.0','{}','validated') RETURNING id"
    )).scalar_one()
    s.execute(text(
        "INSERT INTO quant.validation_reports "
        "(strategy_id, backtest_id, checklist, verdict, reasons) "
        "VALUES (:sid, NULL, '{}', 'reject', 'gate failed')"), {"sid": sid})
    audit = PostgresAuditLog(s, CLOCK)
    with pytest.raises(ValueError, match="refusing paper transition"):
        transition_to_paper(s, audit, strategy_id=str(sid),
                            approved_by="test principal",
                            decision_ref="ADR-test", clock=CLOCK)
    # No override/force/skip flag exists on the promotion path.
    with pytest.raises(TypeError):
        transition_to_paper(s, audit, strategy_id=str(sid),  # type: ignore[call-arg]
                            approved_by="test principal",
                            decision_ref="ADR-test", clock=CLOCK, force=True)


def test_p0_stale_pre_downgrade_approval_cannot_re_promote(pg_session):
    """ADR-0018 freshness: a strategy downgraded to research_shadow (shadowed_at
    set) cannot be re-promoted on the STALE approve report that predates the
    downgrade — a NEW signed artifact created after shadowed_at is mandatory."""
    s = pg_session
    _clean(s)
    shadowed_at = CLOCK.now()
    sid = s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, state, "
        " shadowed_at) VALUES ('momentum','trend_rs_vol','1.0.0','{}',"
        " 'validated', :sh) RETURNING id"), {"sh": shadowed_at}).scalar_one()
    # Stale approve report: created BEFORE the downgrade.
    s.execute(text(
        "INSERT INTO quant.validation_reports "
        "(strategy_id, backtest_id, checklist, verdict, reasons, created_at) "
        "VALUES (:sid, NULL, '{}', 'approve', '', :ca)"),
        {"sid": sid, "ca": shadowed_at - timedelta(days=7)})
    with pytest.raises(ValueError, match="predates the research_shadow"):
        require_signed_validation_artifact(s, str(sid))
    # A NEW approve report created after the downgrade satisfies the gate.
    s.execute(text(
        "INSERT INTO quant.validation_reports "
        "(strategy_id, backtest_id, checklist, verdict, reasons, created_at) "
        "VALUES (:sid, NULL, '{}', 'approve', '', :ca)"),
        {"sid": sid, "ca": shadowed_at + timedelta(days=1)})
    require_signed_validation_artifact(s, str(sid))  # no raise


# ---------------------------------------------------------------------------
# ADR-0016 lineage coherence: the n-consistency check compares the gate's
# n_trials against the LINEAGE count the gate must have deflated at.
# ---------------------------------------------------------------------------

def _momentum_lineage_of_five(s) -> None:
    """Five momentum-lineage trials across family names; the fresh variant
    'xsmom-impl500-tr' holds exactly ONE of them."""
    for fam in ("momentum", "xsmom", "xsmom-pit-tr", "xsmom-impl-tr",
                "xsmom-impl500-tr"):
        register_trial(s, family=fam, lineage="momentum",
                       spec={"fam": fam}, metrics={})
    s.commit()


def test_approval_accepts_gate_deflated_at_lineage_count(pg_session):
    s = pg_session
    _clean(s)
    _momentum_lineage_of_five(s)
    wf = walk_forward(regime_series(), lambda b, t: momentum_v1,
                      k=4, horizon=40, embargo=10, warmup=60)
    gate = GateReport(strategy_return=1.02, bh_return=0.12, null_p_value=0.0,
                      dsr=1.0, n_trials=5, passed=True, reasons=[])
    d = evaluate_approval(s, family="xsmom-impl500-tr", lineage="momentum",
                          gate=gate, wf=wf, oos_untouched_attested=True)
    assert d.approved


def test_approval_refuses_gate_deflated_at_family_count(pg_session):
    """The counting defect itself: a first-in-family gate (n_trials=1) may no
    longer clear approval when the lineage holds more trials."""
    s = pg_session
    _clean(s)
    _momentum_lineage_of_five(s)
    gate = GateReport(strategy_return=1.02, bh_return=0.12, null_p_value=0.0,
                      dsr=1.0, n_trials=1, passed=True, reasons=[])
    d = evaluate_approval(s, family="xsmom-impl500-tr", lineage="momentum",
                          gate=gate, wf=None, oos_untouched_attested=True)
    assert not d.approved
    assert any("lineage 'momentum' has 5" in r for r in d.reasons)
