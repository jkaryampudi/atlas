"""Shadow model-upgrade comparison (Constitution 7.2; atlas/agents/
shadow_compare.py) — StubClient end to end, NO live calls anywhere.

The load-bearing assertion is NON-ACTIONABILITY: a comparison re-runs the full
committee path on the challenger, and NOTHING it produces may reach
research.memos (the table the console, the eval --db mode and the future
memo->proposal bridge read) — shadow outputs land only in
research.shadow_memos, and every agent_run the comparison writes is marked
shadow=true. Alongside it: evidence reconstruction is byte-verbatim from
research.memo_evidence; both cohorts are scored with the REAL eval metrics;
the 'shadow' budget surface binds under the global breaker and a mid-cohort
BudgetExhausted halts cleanly with partial results reported honestly; the
audit chain records the comparison with counts + cost.

Seeding uses the production write path (committee_memo, stub client), so the
incumbent cohort is exactly what the desk persists — the same discipline as
test_memo_eval_db_pg.py.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

import atlas.agents.shadow_compare as sc
from atlas.agents.roles.cio import committee_memo
from atlas.agents.roles.debate import DebateResult
from atlas.agents.runtime.llm import StubClient
from atlas.agents.runtime.runner import SCHEMA_MAX_ATTEMPTS
from atlas.agents.schemas.debate import DebateCase
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from tests.conftest import URL, requires_pg

pytestmark = requires_pg

CLOCK = FrozenClock(datetime(2026, 7, 15, 6, 0, tzinfo=UTC))
CHALLENGER = "claude-sonnet-5"


@pytest.fixture(autouse=True)
def _budget_env(monkeypatch):
    monkeypatch.setenv("ATLAS_DAILY_LLM_BUDGET_USD", "10.0")
    monkeypatch.delenv("ATLAS_BUDGET_SHADOW", raising=False)
    monkeypatch.delenv("ATLAS_MODEL_DEFAULT", raising=False)


def _audit(s):
    return PostgresAuditLog(s, CLOCK)


# ---------------------------------------------------------------------------
# Incumbent cohort seeds (production write path, stub client)

EVIDENCE_A = [
    ("quant:families:SHQA:2026-07-12",
     "Quant validation record: family momentum-v1 FAILED decision gates on real "
     "data; family xsmom-pit holds a decision-grade PASS; SHQA is not in the "
     "winner decile."),
    ("dcp:bars:SHQA:2026-07-12",
     "SHQA daily closes (EODHD vendor bars, split-adjusted): persistent strength "
     "over the recent window, closing above both moving averages."),
]
EVIDENCE_B = [
    ("dcp:bars:SHQB:2026-07-12",
     "SHQB daily closes (EODHD vendor bars, split-adjusted): trend evidence "
     "present over the recent window."),
]

GOOD_MEMO_A = json.dumps({
    "recommendation": "REJECT", "conviction": "MEDIUM",
    "thesis": "The committee rejects SHQA for new capital because no validated "
              "strategy covers the name and the momentum family failed its "
              "gates on real data.",
    "kill_criteria": [
        "SHQA enters the winner decile at a monthly rebalance",
        "The close falls below the SMA50 for five consecutive sessions"],
    "evidence_refs": ["quant:families:SHQA:2026-07-12",
                      "dcp:bars:SHQA:2026-07-12"],
    "dissent": "Rejecting here risks whipsawing the watchlist: the point-in-time "
               "machinery is close to selecting the name and persistent "
               "structure argues the entry disappears while the desk waits.",
    "debate_summary": "The bull rested on trend persistence, the bear on the "
                      "absence of validated coverage; the committee weighed "
                      "the bear case as decisive."})

VACUOUS_THESIS_B = ("The committee rejects SHQB because no validated strategy "
                    "covers the name and trend strength alone cannot justify "
                    "deployment of capital.")
VACUOUS_MEMO_B = json.dumps({
    "recommendation": "REJECT", "conviction": "LOW",
    "thesis": VACUOUS_THESIS_B,
    "kill_criteria": [
        "The close falls below the SMA50 for five consecutive sessions",
        "The family verdict is revoked at a quarterly review"],
    "evidence_refs": ["dcp:bars:SHQB:2026-07-12"],
    # a restatement: passes the production cage, fails the harness
    "dissent": "Some disagree, but " + VACUOUS_THESIS_B,
    "debate_summary": "Both sides argued from the same trend block; the "
                      "committee weighed validated coverage as decisive."})


def _case(stance: str, *points: str, weakest: str, concede: str) -> DebateCase:
    return DebateCase(stance=stance, strongest_points=list(points),
                      weakest_opposing_point=weakest, concede=concede,
                      evidence_refs=[])


def _debate() -> DebateResult:
    bull = _case("BULL",
                 "Persistent trend above both moving averages shows accumulation",
                 "Relative strength through a flat tape argues real demand",
                 "The validated family proves momentum edges exist",
                 weakest="The bear leans entirely on gate verdicts",
                 concede="No validated strategy covers the name today.")
    bear = _case("BEAR",
                 "Gate failures are the reason the fund gates strategies",
                 "The winner decile excludes this symbol entirely",
                 "Buying ungated exposure is mandate drift",
                 weakest="Accumulation is inference from price alone",
                 concede="The trend structure is real and has persisted.")
    return DebateResult(bull=bull, bear=bear,
                        bull_rebuttal=bull, bear_rebuttal=bear)


def _seed_cohort(s) -> dict[str, str]:
    """Two committee memos through the production write path, with evidence +
    debate provenance. SHQA is made older so the cohort order (created_at
    DESC) is deterministic: [SHQB, SHQA]. Returns symbol -> memo id."""
    committee_memo(session=s, audit=_audit(s), client=StubClient([GOOD_MEMO_A]),
                   symbol="SHQA", question="what now?", evidence=EVIDENCE_A,
                   debate=_debate())
    committee_memo(session=s, audit=_audit(s), client=StubClient([VACUOUS_MEMO_B]),
                   symbol="SHQB", question="what now?", evidence=EVIDENCE_B,
                   debate=_debate())
    s.execute(text("UPDATE research.memos SET created_at = created_at "
                   "- interval '1 hour' WHERE instrument_symbol = 'SHQA'"))
    s.commit()
    rows = s.execute(text(
        "SELECT instrument_symbol AS sym, CAST(id AS text) AS id "
        "FROM research.memos")).all()
    return {r.sym: r.id for r in rows}


# ---------------------------------------------------------------------------
# Challenger stub script (call order per memo: bull, bear, bull_rebuttal,
# bear_rebuttal, cio — no specialists: no dcp:signal: block in this evidence)

BULL_JSON = json.dumps({
    "stance": "BULL", "strongest_points": [
        "Accumulation shows in the persistent closes above both averages",
        "Demand is real: relative strength held through a flat tape",
        "Momentum edges exist and the validated family proves the class"],
    "weakest_opposing_point": "The bear leans entirely on gate verdicts",
    "evidence_refs": [], "concede": "No validated strategy covers the name today."})
BEAR_JSON = json.dumps({
    "stance": "BEAR", "strongest_points": [
        "Gate failures are the reason the fund gates strategies",
        "The winner decile excludes this symbol entirely",
        "Buying ungated exposure is mandate drift"],
    "weakest_opposing_point": "Accumulation is inference from price alone",
    "evidence_refs": [], "concede": "The trend structure is real and has persisted."})

# challenger memo for SHQB: FIXES the incumbent's vacuous dissent
CHAL_GOOD_B = json.dumps({
    "recommendation": "REJECT", "conviction": "LOW",
    "thesis": VACUOUS_THESIS_B,
    "kill_criteria": [
        "The close falls below the SMA50 for five consecutive sessions",
        "The family verdict is revoked at a quarterly review"],
    "evidence_refs": ["dcp:bars:SHQB:2026-07-12"],
    "dissent": "Waiting surrenders the entry: the point-in-time machinery is "
               "close to selecting this name and persistent structure argues "
               "the setup disappears while the desk hesitates on coverage.",
    "debate_summary": "The bull argued persistence, the bear argued mandate "
                      "drift; the committee weighed coverage as decisive."})

# challenger memo for SHQA: a REGRESSION — dissent restates the thesis
CHAL_THESIS_A = ("The committee rejects SHQA for new capital because no "
                 "validated strategy covers the name and the momentum family "
                 "failed its gates on real data.")
CHAL_VACUOUS_A = json.dumps({
    "recommendation": "REJECT", "conviction": "LOW",
    "thesis": CHAL_THESIS_A,
    "kill_criteria": [
        "SHQA enters the winner decile at a monthly rebalance",
        "The close falls below the SMA50 for five consecutive sessions"],
    "evidence_refs": ["dcp:bars:SHQA:2026-07-12"],
    "dissent": "Some disagree, but " + CHAL_THESIS_A,
    "debate_summary": "Both sides argued the same trend block; the committee "
                      "weighed validated coverage as decisive."})

DEBATE_SCRIPT = [BULL_JSON, BEAR_JSON, BULL_JSON, BEAR_JSON]
FULL_SCRIPT = (DEBATE_SCRIPT + [CHAL_GOOD_B]        # SHQB first (most recent)
               + DEBATE_SCRIPT + [CHAL_VACUOUS_A])  # then SHQA


def _run(s, stub: StubClient | None = None) -> sc.ShadowComparison:
    return sc.run_shadow_comparison(
        s, CLOCK, n_memos=8, challenger_model=CHALLENGER,
        client=stub or StubClient(list(FULL_SCRIPT)))


# ---------------------------------------------------------------------------
def test_shadow_outputs_land_only_in_shadow_memos_memos_unchanged(clean_audit):
    """THE non-actionable test (Constitution 7.2): research.memos is untouched
    by a comparison; shadow outputs exist only in research.shadow_memos; every
    agent_run the comparison writes is marked shadow=true."""
    s = clean_audit
    _seed_cohort(s)
    memos_before = s.execute(text("SELECT count(*) FROM research.memos")).scalar()
    nonshadow_before = s.execute(text(
        "SELECT count(*) FROM research.agent_runs WHERE NOT shadow")).scalar()

    comp = _run(s)

    assert [o.status for o in comp.outcomes] == ["ok", "ok"]
    assert s.execute(text("SELECT count(*) FROM research.memos")).scalar() \
        == memos_before                       # NOTHING reached production memos
    assert s.execute(text(
        "SELECT count(*) FROM research.agent_runs WHERE NOT shadow")).scalar() \
        == nonshadow_before                   # every new run is shadow-marked
    assert s.execute(text(
        "SELECT count(*) FROM research.agent_runs WHERE shadow")).scalar() == 10
    rows = s.execute(text(
        "SELECT challenger_model, comparison_id FROM research.shadow_memos")).all()
    assert len(rows) == 2
    assert all(r.challenger_model == CHALLENGER for r in rows)
    assert all(r.comparison_id == comp.comparison_id for r in rows)
    # the audit chain stays verifiable with the comparison events on it
    assert PostgresAuditLog(s, CLOCK).verify() > 0


def test_evidence_reconstruction_is_verbatim(clean_audit):
    """Byte-compare: the corpus the challenger reads is EXACTLY what
    research.memo_evidence persisted for the source memo — and it reaches the
    prompts unmodified."""
    s = clean_audit
    ids = _seed_cohort(s)
    assert sc.reconstruct_evidence(s, ids["SHQA"]) == EVIDENCE_A   # byte-equal
    assert sc.reconstruct_evidence(s, ids["SHQB"]) == EVIDENCE_B

    stub = StubClient(list(FULL_SCRIPT))
    _run(s, stub)
    # prompts 0-4 are SHQB (most recent first), 5-9 are SHQA;
    # per memo: bull, bear, bull_reb, bear_reb, cio
    shqb_bull, shqa_bull, shqa_cio = stub.prompts[0], stub.prompts[5], stub.prompts[9]
    for ref, body in EVIDENCE_B:
        assert f"DCP evidence [{ref}]: {body}" in shqb_bull
    for ref, body in EVIDENCE_A:
        assert f"DCP evidence [{ref}]: {body}" in shqa_bull
        assert f"DCP evidence [{ref}]: {body}" in shqa_cio
    assert "Structured debate" in shqa_cio      # full committee context reached the CIO


def test_both_cohorts_scored_with_the_real_eval_metrics(clean_audit):
    """score_bundle runs on both cohorts, same metrics, same thresholds — and
    the verdicts move independently: the incumbent's vacuous SHQB dissent
    FAILS while the challenger's fixed one PASSES, and the challenger's
    regressed SHQA dissent FAILS while the incumbent's PASSES."""
    s = clean_audit
    ids = _seed_cohort(s)
    comp = _run(s)

    inc = {b.bundle_id: b for b in comp.incumbent_scores}
    chal = {b.bundle_id: b for b in comp.challenger_scores}
    assert set(inc) == {ids["SHQA"], ids["SHQB"]}
    assert set(chal) == {f"shadow:{ids['SHQA']}", f"shadow:{ids['SHQB']}"}
    metric_names = {r.name for b in (*inc.values(), *chal.values())
                    for r in b.results}
    assert metric_names == {"grounding", "kill_observability",
                            "dissent_distinctness", "debate_diversity",
                            "conviction_conformance", "refs_completeness"}

    def result(b, name):
        return {r.name: r for r in b.results}[name]

    assert inc[ids["SHQA"]].passed is True
    assert result(inc[ids["SHQB"]], "dissent_distinctness").passed is False
    assert result(chal[f"shadow:{ids['SHQB']}"], "dissent_distinctness").passed is True
    assert result(chal[f"shadow:{ids['SHQA']}"], "dissent_distinctness").passed is False
    # the challenger's debate provenance is scored too (distinct opening cases)
    assert result(chal[f"shadow:{ids['SHQB']}"], "debate_diversity").passed is True


def test_costs_attributed_per_memo_and_audited(clean_audit):
    s = clean_audit
    ids = _seed_cohort(s)
    comp = _run(s)
    # challenger full-path cost: positive for both memos (stub bills at the
    # fail-closed rate, real spend on the shared tally)
    assert set(comp.challenger_cost_usd) == {ids["SHQA"], ids["SHQB"]}
    assert all(c > 0 for c in comp.challenger_cost_usd.values())
    # incumbent attributable cost: the CIO run linked via memos.agent_run_id
    assert set(comp.incumbent_cio_cost_usd) == {ids["SHQA"], ids["SHQB"]}
    assert all(c > 0 for c in comp.incumbent_cio_cost_usd.values())
    payload = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'shadow.comparison.completed'")).scalar_one()
    assert payload["challenger_model"] == CHALLENGER
    assert payload["n_cohort"] == 2
    assert payload["outcomes"] == {"ok": 2}
    assert payload["halted"] is False
    assert payload["challenger_cost_usd"] == pytest.approx(
        sum(comp.challenger_cost_usd.values()), abs=1e-6)


def test_budget_surface_binds_and_halts_cleanly_partial_reported(clean_audit):
    """The 'shadow' sub-cap (default $3.00) binds inside the global breaker:
    with the day's tally already past it, the first challenger call is killed,
    the comparison halts, the untouched memos are recorded not-attempted, and
    the partial report says so honestly. research.memos stays untouched."""
    s = clean_audit
    _seed_cohort(s)
    s.execute(text(
        "INSERT INTO research.agent_runs (agent_role, prompt_template_hash, "
        " model, status, cost_usd) VALUES ('seed', 'h', 'stub', 'ok', 3.50)"))
    s.commit()
    memos_before = s.execute(text("SELECT count(*) FROM research.memos")).scalar()

    comp = _run(s)

    assert comp.halted is True
    assert [o.status for o in comp.outcomes] == ["budget_halt", "not_attempted"]
    assert "shadow" in comp.outcomes[0].detail          # the surface that fired
    assert s.execute(text("SELECT count(*) FROM research.shadow_memos")).scalar() == 0
    assert s.execute(text("SELECT count(*) FROM research.memos")).scalar() \
        == memos_before
    # the killed attempt is recorded — and marked shadow like every other run
    # this comparison wrote (the budget_kill row carries the flag too)
    kill = s.execute(text(
        "SELECT shadow FROM research.agent_runs "
        "WHERE status = 'budget_kill'")).scalar_one()
    assert kill is True
    report = sc.render_report(comp)
    assert "PARTIAL RESULTS" in report
    assert "not_attempted".upper() in report or "NOT_ATTEMPTED" in report
    payload = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'shadow.comparison.completed'")).scalar_one()
    assert payload["halted"] is True
    assert payload["outcomes"] == {"budget_halt": 1, "not_attempted": 1}


def test_budget_env_override_tightens_the_shadow_surface(clean_audit, monkeypatch):
    s = clean_audit
    _seed_cohort(s)
    monkeypatch.setenv("ATLAS_BUDGET_SHADOW", "0.10")
    s.execute(text(
        "INSERT INTO research.agent_runs (agent_role, prompt_template_hash, "
        " model, status, cost_usd) VALUES ('seed', 'h', 'stub', 'ok', 0.20)"))
    s.commit()
    comp = _run(s)
    assert comp.halted is True
    assert "ATLAS_BUDGET_SHADOW" in comp.outcomes[0].detail


def test_refuses_a_self_comparison(clean_audit):
    """challenger == incumbent default isolates nothing: refused at the API
    and at the CLI, before any client or call exists."""
    s = clean_audit
    with pytest.raises(ValueError, match="incumbent default"):
        sc.run_shadow_comparison(s, CLOCK, n_memos=8,
                                 challenger_model="claude-sonnet-4-6",
                                 client=StubClient([]))
    assert sc.main(["--model", "claude-sonnet-4-6", "--database-url", URL]) == 2


def test_refuses_live_client_without_api_key(clean_audit, monkeypatch):
    """No injected client + no API key must refuse BEFORE any network call —
    the no-live-calls guarantee for tests and misconfigured hosts."""
    s = clean_audit
    _seed_cohort(s)
    monkeypatch.delenv("ATLAS_ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ATLAS_ANTHROPIC_API_KEY"):
        sc.run_shadow_comparison(s, CLOCK, n_memos=8,
                                 challenger_model=CHALLENGER)


def test_cohort_requires_full_provenance(clean_audit):
    """Memos without evidence bodies or without debate rows are NOT in the
    cohort: their committee path cannot be replayed verbatim."""
    s = clean_audit
    ids = _seed_cohort(s)
    # strip SHQB's debate rows -> it drops out of the cohort
    s.execute(text("DELETE FROM research.memo_debate WHERE CAST(memo_id AS text) "
                   "= :m"), {"m": ids["SHQB"]})
    s.commit()
    cohort = sc.select_cohort(s, n_memos=8)
    assert [m for m, _ in cohort] == [ids["SHQA"]]


def test_cli_end_to_end_writes_report_and_honest_verdict(clean_audit,
                                                         monkeypatch, tmp_path,
                                                         capsys):
    s = clean_audit
    _seed_cohort(s)
    monkeypatch.setattr(sc, "REPORTS_DIR", tmp_path)
    rc = sc.main(["--n", "8", "--model", CHALLENGER, "--database-url", URL],
                 client=StubClient(list(FULL_SCRIPT)))
    assert rc == 0
    out = capsys.readouterr().out
    reports = list(tmp_path.glob("shadow-model-comparison-*.md"))
    assert len(reports) == 1
    content = reports[0].read_text()
    for txt in (content, out):
        assert "FLOOR-CHECK, not a ranking oracle" in txt
        assert "human read" in txt                  # the switch needs a human
        assert "Principal-reviewed registry change" in txt
        assert "CIO runs ONLY" in txt               # cost honesty
    # the comparison persisted (CLI commits): shadow rows exist, memos untouched
    assert s.execute(text("SELECT count(*) FROM research.shadow_memos")).scalar() == 2
    assert s.execute(text("SELECT count(*) FROM research.memos")).scalar() == 2


def test_cli_fails_closed_on_empty_cohort(clean_audit, capsys):
    """No memos with full provenance -> nothing compared -> exit 1 (a
    comparison that compared nothing must not read as success). No client is
    even needed: nothing runs."""
    rc = sc.main(["--model", CHALLENGER, "--database-url", URL])
    assert rc == 1
    assert "failing closed" in capsys.readouterr().err


def test_cli_requires_explicit_model():
    with pytest.raises(SystemExit) as e:
        sc.main(["--n", "4"])
    assert e.value.code == 2


def test_specialist_panel_runs_in_shadow_for_signal_names(clean_audit):
    """Evidence carrying a dcp:signal: block routes through the specialist
    panel exactly like the desk — all three seats on the challenger client,
    shadow-marked, persisted in the payload."""
    s = clean_audit
    evidence = [
        ("dcp:signal:xsmom:SHQC:2026-07-12",
         "Signal record: SHQC is a candidate in the xsmom-pit lane for the "
         "current rebalance window."),
        ("dcp:fundamentals:SHQC:2026-07-12",
         "Fundamentals block: reported revenue grew year over year with "
         "stable margins per the vendor record."),
        ("dcp:regime:2026-07-12",
         "Regime classifier: the current market regime is risk-on trend."),
    ]
    committee_memo(session=s, audit=_audit(s),
                   client=StubClient([json.dumps({
                       "recommendation": "WATCHLIST", "conviction": "LOW",
                       "thesis": "Signal coverage exists but the desk waits for "
                                 "the committee to weigh the fundamentals.",
                       "kill_criteria": [
                           "SHQC exits the winner decile at a monthly rebalance",
                           "Reported revenue declines in the next filing"],
                       "evidence_refs": ["dcp:signal:xsmom:SHQC:2026-07-12"],
                       "dissent": "The validated lane already argues for entry "
                                  "and waiting may surrender the setup window.",
                       "debate_summary": "Bull argued lane coverage, bear "
                                         "argued thin fundamentals history."})]),
                   symbol="SHQC", question="what now?", evidence=evidence,
                   debate=_debate())
    s.commit()
    specialist = json.dumps({"stance": "neutral",
                             "key_points": ["The lane evidence is present",
                                            "History depth remains limited"],
                             "red_flags": [], "confidence": "low"})
    # call order: bull, bear, bull_reb, bear_reb, quality, growth, macro, cio
    stub = StubClient(DEBATE_SCRIPT + [specialist, specialist, specialist,
                                       CHAL_GOOD_B.replace("SHQB", "SHQC")])
    comp = sc.run_shadow_comparison(s, CLOCK, n_memos=8,
                                    challenger_model=CHALLENGER, client=stub)
    assert [o.status for o in comp.outcomes] == ["ok"]
    payload = s.execute(text(
        "SELECT payload FROM research.shadow_memos")).scalar_one()
    assert set(payload["specialists"]["assessments"]) == {"quality", "growth",
                                                          "macro"}
    roles = {r.agent_role for r in s.execute(text(
        "SELECT agent_role FROM research.agent_runs WHERE shadow")).all()}
    assert {"quality_analyst", "growth_analyst", "macro_analyst",
            "debate_bull", "debate_bear", "cio"} <= roles
    assert s.execute(text("SELECT count(*) FROM research.memos")).scalar() == 1


def test_desk_fail_soft_semantics_cage_hold_recorded_per_memo(clean_audit):
    """A challenger whose CIO output fails the cage on every attempt
    (SCHEMA_MAX_ATTEMPTS, currently 3) is a CAGE HOLD for that memo — recorded
    honestly, the rest of the cohort continues, and no shadow row is persisted
    for the held memo."""
    s = clean_audit
    ids = _seed_cohort(s)
    bad_cio = json.dumps({"recommendation": "REJECT"})   # fails CommitteeMemo
    stub = StubClient(DEBATE_SCRIPT + [bad_cio] * SCHEMA_MAX_ATTEMPTS  # SHQB: cage hold
                      + DEBATE_SCRIPT + [CHAL_VACUOUS_A])              # SHQA: lands
    comp = sc.run_shadow_comparison(s, CLOCK, n_memos=8,
                                    challenger_model=CHALLENGER, client=stub)
    by_id = {o.source_memo_id: o for o in comp.outcomes}
    assert by_id[ids["SHQB"]].status == "cage_hold"
    assert by_id[ids["SHQA"]].status == "ok"
    persisted = s.execute(text(
        "SELECT CAST(source_memo_id AS text) FROM research.shadow_memos")).scalars().all()
    assert persisted == [ids["SHQA"]]
    # held memo still shows its real spend (failed attempts cost money)
    assert comp.challenger_cost_usd[ids["SHQB"]] > 0
    # the incumbent side of the held memo is still scored — the report shows
    # the side-by-side with the hold stated, never a silent drop
    assert {b.bundle_id for b in comp.incumbent_scores} == set(ids.values())
    report = sc.render_report(comp)
    assert "CAGE_HOLD" in report
