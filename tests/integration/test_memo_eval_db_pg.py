"""--db mode of the memo-quality eval harness (desk-review 2026-07 item 8):
scores REAL persisted memos read back from research.memos + memo_evidence +
memo_debate, strictly read-only — no writes, no audit events.

Memos are persisted through committee_memo (the production write path, stub
client), so the read-back is proven against exactly what the desk persists —
VERBATIM for every column load_db_bundles maps (eval-harness review 2026-07
finding #18: a thesis<->dissent column swap once passed this whole file),
including the 0019 debate provenance and its honest absence on memos that
predate it, the pre-0013 evidence shape (finding #16), and the fail-closed
--strict exit on an empty result set (finding #2).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import text

from atlas.agents.evals.run import load_db_bundles, main, report_dict, run_db
from atlas.agents.roles.cio import committee_memo
from atlas.agents.roles.debate import DebateResult
from atlas.agents.runtime.llm import StubClient
from atlas.agents.schemas.debate import DebateCase
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from tests.conftest import URL, requires_pg

pytestmark = requires_pg


def _audit(s):
    return PostgresAuditLog(s, FrozenClock(datetime(2026, 7, 12, 6, 0, tzinfo=UTC)))


EVIDENCE_A = [
    ("quant:families:EVQA:2026-07-12",
     "Quant validation record: family momentum-v1 FAILED decision gates on real "
     "data; family xsmom-pit holds a decision-grade PASS; EVQA is not in the "
     "winner decile."),
    ("dcp:bars:EVQA:2026-07-12",
     "EVQA daily closes (EODHD vendor bars, split-adjusted): persistent strength "
     "over the recent window, closing above both moving averages."),
]

GOOD_MEMO_DICT = {
    "recommendation": "REJECT", "conviction": "MEDIUM",
    "thesis": "The committee rejects EVQA for new capital because no validated "
              "strategy covers the name and the momentum family failed its "
              "gates on real data.",
    "kill_criteria": [
        "EVQA enters the winner decile at a monthly rebalance",
        "The close falls below the SMA50 for five consecutive sessions"],
    "evidence_refs": ["quant:families:EVQA:2026-07-12",
                      "dcp:bars:EVQA:2026-07-12"],
    "dissent": "Rejecting here risks whipsawing the watchlist: the point-in-time "
               "machinery is close to selecting the name and persistent "
               "structure argues the entry disappears while the desk waits.",
    "debate_summary": "The bull rested on trend persistence, the bear on the "
                      "absence of validated coverage; the committee weighed "
                      "the bear case as decisive."}
GOOD_MEMO = json.dumps(GOOD_MEMO_DICT)

VACUOUS_THESIS = ("The committee rejects EVQB because no validated strategy "
                  "covers the name and trend strength alone cannot justify "
                  "deployment of capital.")
VACUOUS_MEMO_DICT = {
    "recommendation": "REJECT", "conviction": "LOW",
    "thesis": VACUOUS_THESIS,
    "kill_criteria": [
        "The close falls below the SMA50 for five consecutive sessions",
        "The family verdict is revoked at a quarterly review"],
    "evidence_refs": ["dcp:bars:EVQB:2026-07-12"],
    # a restatement: passes the production cage, must fail the harness
    "dissent": "Some disagree, but " + VACUOUS_THESIS,
    "debate_summary": ""}
VACUOUS_MEMO = json.dumps(VACUOUS_MEMO_DICT)

EVIDENCE_B = [("dcp:bars:EVQB:2026-07-12",
               "EVQB daily closes (EODHD vendor bars, split-adjusted): trend "
               "evidence present over the recent window.")]

# the PROMPT-CONFORMANT honest abstention (round-2 findings #6/#7): the CIO
# template caps conviction at LOW when evidence_available=false and never
# offers N/A, so this — not the hand-written N/A fixture shape — is what the
# production pipeline actually persists. It must PASS in --db mode.
IE_LOW_MEMO = json.dumps({
    "recommendation": "INSUFFICIENT_EVIDENCE", "conviction": "LOW",
    "thesis": "No DCP evidence was provided for this candidate; the desk "
              "declines to argue a direction it cannot ground.",
    "kill_criteria": [], "evidence_refs": [],
    "dissent": "Not applicable.", "debate_summary": ""})

# refs cited by the memo while the runtime attached NOTHING: passes the cage
# today (REJECT + LOW + digit-free), and must keep FAILING the judge — the
# pre-0013 n/a must never become a fabricated-refs bypass (finding #16).
FABRICATED_REFS_MEMO = json.dumps({
    "recommendation": "REJECT", "conviction": "LOW",
    "thesis": "The committee rejects EVQC because no validated strategy covers "
              "the name and the desk declines ungated exposure.",
    "kill_criteria": [
        "EVQC enters the winner decile at a monthly rebalance",
        "The close falls below the SMA50 for five consecutive sessions"],
    "evidence_refs": ["dcp:ghost:EVQC:2026-07-12"],
    "dissent": "Persistent relative strength argues the machinery will select "
               "this name shortly and waiting surrenders the whole entry.",
    "debate_summary": ""})


def _case(stance: str, *points: str, weakest: str, concede: str) -> DebateCase:
    return DebateCase(stance=stance, strongest_points=list(points),
                      weakest_opposing_point=weakest, concede=concede,
                      evidence_refs=["quant:families:EVQA:2026-07-12"])


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


def _persist_memos(s) -> None:
    committee_memo(session=s, audit=_audit(s), client=StubClient([GOOD_MEMO]),
                   symbol="EVQA", question="what now?", evidence=EVIDENCE_A,
                   debate=_debate())
    committee_memo(session=s, audit=_audit(s), client=StubClient([VACUOUS_MEMO]),
                   symbol="EVQB", question="what now?", evidence=EVIDENCE_B)
    s.commit()


def _assert_side_matches(side, case: DebateCase) -> None:
    assert side is not None
    assert side.strongest_points == tuple(case.strongest_points)
    assert side.weakest_opposing_point == case.weakest_opposing_point
    assert side.concede == case.concede


def test_db_mode_reads_back_exactly_what_the_desk_persisted(clean_audit):
    """Finding #18: EVERY column load_db_bundles maps is asserted VERBATIM
    against what committee_memo persisted — a swapped pair of bind params or
    constructor args anywhere in the read path must fail here."""
    s = clean_audit
    _persist_memos(s)
    bundles = {b.symbol: b for b in load_db_bundles(s, since=None, limit=10)}
    a, b = bundles["EVQA"], bundles["EVQB"]

    assert a.recommendation == GOOD_MEMO_DICT["recommendation"]
    assert a.conviction == GOOD_MEMO_DICT["conviction"]
    assert a.thesis == GOOD_MEMO_DICT["thesis"]
    assert a.kill_criteria == tuple(GOOD_MEMO_DICT["kill_criteria"])
    assert a.evidence_refs == tuple(GOOD_MEMO_DICT["evidence_refs"])
    assert a.dissent == GOOD_MEMO_DICT["dissent"]
    assert a.debate_summary == GOOD_MEMO_DICT["debate_summary"]
    assert a.evidence == tuple(EVIDENCE_A)          # verbatim, ordinal order
    assert a.run_attached_evidence is True
    assert a.debate is not None                      # 0019 provenance read back
    expected = _debate()
    _assert_side_matches(a.debate.bull, expected.bull)
    _assert_side_matches(a.debate.bear, expected.bear)
    _assert_side_matches(a.debate.bull_rebuttal, expected.bull_rebuttal)
    _assert_side_matches(a.debate.bear_rebuttal, expected.bear_rebuttal)

    assert b.recommendation == VACUOUS_MEMO_DICT["recommendation"]
    assert b.conviction == VACUOUS_MEMO_DICT["conviction"]
    assert b.thesis == VACUOUS_MEMO_DICT["thesis"]
    assert b.kill_criteria == tuple(VACUOUS_MEMO_DICT["kill_criteria"])
    assert b.evidence_refs == tuple(VACUOUS_MEMO_DICT["evidence_refs"])
    assert b.dissent == VACUOUS_MEMO_DICT["dissent"]
    assert b.debate_summary == VACUOUS_MEMO_DICT["debate_summary"]
    assert b.evidence == tuple(EVIDENCE_B)
    assert b.debate is None                          # no rows -> honest None


def test_db_mode_scores_are_computed_and_read_only(clean_audit):
    s = clean_audit
    _persist_memos(s)
    before = {t: s.execute(text(f"SELECT count(*) FROM {t}")).scalar()
              for t in ("audit.decision_events", "research.memos",
                        "research.memo_evidence", "research.memo_debate",
                        "research.agent_runs")}
    scores = {sc.symbol: sc for sc in run_db(URL, since=None, limit=10)}
    assert scores["EVQA"].passed is True
    diversity = {r.name: r for r in scores["EVQA"].results}["debate_diversity"]
    assert diversity.passed is True                  # distinct opening cases
    vac = {r.name: r for r in scores["EVQB"].results}
    assert vac["dissent_distinctness"].passed is False
    assert vac["debate_diversity"].passed is None    # predates-0019 shape
    assert scores["EVQB"].passed is False
    after = {t: s.execute(text(f"SELECT count(*) FROM {t}")).scalar()
             for t in before}
    assert after == before                           # measurement, not action


def test_db_mode_pre_0013_shape_is_not_scoreable_never_spurious_fails(clean_audit):
    """Finding #16: a compliant memo whose evidence bodies predate migration
    0013 (refs persisted, run provably attached evidence, zero memo_evidence
    rows) is reported n/a on the evidence-dependent metrics and counted in
    the report — never accused of fabrication."""
    s = clean_audit
    _persist_memos(s)
    # simulate the pre-0013 row: bodies were never persisted (the table did
    # not exist); everything else about the memo is exactly as the desk wrote
    s.execute(text(
        "DELETE FROM research.memo_evidence WHERE memo_id = "
        "(SELECT id FROM research.memos WHERE instrument_symbol = 'EVQA')"))
    s.commit()
    scores = {sc.symbol: sc for sc in run_db(URL, since=None, limit=10)}
    by_name = {r.name: r for r in scores["EVQA"].results}
    assert by_name["grounding"].passed is None
    assert "0013" in by_name["grounding"].detail
    assert by_name["refs_completeness"].passed is None
    assert by_name["conviction_conformance"].passed is True   # MEDIUM not capped
    assert scores["EVQA"].passed is True             # no spurious FAILs
    report = report_dict(list(scores.values()))
    assert report["not_scoreable_pre_0013"] == 1


def test_db_mode_prompt_conformant_abstention_passes(clean_audit):
    """Round-2 findings #6/#7 (dissent rule rescoped round-4, round-3
    finding #7): an INSUFFICIENT_EVIDENCE memo persisted through the
    production write path exactly as the pinned CIO template shapes it
    (conviction LOW — the template never offers N/A — and the empty dissent
    written out loud as 'Not applicable.') must PASS: the old
    na_iff_insufficient XOR and the thin-dissent floor failed 100% of
    spec-compliant abstentions in the very --db mode the harness exists
    for. The dissent column on an abstention is never graded at all now —
    the round-2 marker list failed 'No dissent.' one inch outside it."""
    s = clean_audit
    committee_memo(session=s, audit=_audit(s), client=StubClient([IE_LOW_MEMO]),
                   symbol="EVQD", question="what now?", evidence=None)
    s.commit()
    scores = {sc.symbol: sc for sc in run_db(URL, since=None, limit=10)}
    by_name = {r.name: r for r in scores["EVQD"].results}
    assert by_name["conviction_conformance"].passed is True
    assert by_name["dissent_distinctness"].passed is None   # abstention: n/a
    assert by_name["kill_observability"].passed is None
    assert by_name["refs_completeness"].passed is True      # IE may cite nothing
    assert scores["EVQD"].passed is True


def test_db_mode_fabricated_refs_still_fail_closed(clean_audit):
    """Finding #16's discriminator: same persisted shape (refs, no bodies)
    but the run attached NOTHING -> the refs are the memo's own invention and
    keep failing, so the honest n/a is not a bypass."""
    s = clean_audit
    committee_memo(session=s, audit=_audit(s),
                   client=StubClient([FABRICATED_REFS_MEMO]),
                   symbol="EVQC", question="what now?", evidence=None)
    s.commit()
    scores = {sc.symbol: sc for sc in run_db(URL, since=None, limit=10)}
    by_name = {r.name: r for r in scores["EVQC"].results}
    assert by_name["refs_completeness"].passed is False
    assert "dcp:ghost:EVQC:2026-07-12" in by_name["refs_completeness"].detail
    assert scores["EVQC"].passed is False


def test_db_mode_exit_codes_informational_vs_strict(clean_audit, capsys):
    _persist_memos(clean_audit)
    args = ["--db", "--database-url", URL, "--limit", "10"]
    assert main(args) == 0                           # measurement by default
    assert main([*args, "--strict"]) == 1            # EVQB fails the bar
    out = capsys.readouterr().out
    assert "persisted memos (read-only)" in out
    assert "known v1 limits" in out                  # caveats on every report


def test_db_mode_strict_fails_closed_on_zero_memos(clean_audit, capsys):
    """Finding #2: --strict over an empty result set (mistyped --since, empty
    or wrong database) must exit non-zero — a gate that scored nothing gated
    nothing. Informational mode stays exit 0."""
    _persist_memos(clean_audit)
    args = ["--db", "--database-url", URL, "--limit", "10",
            "--since", "2100-01-01"]
    assert main(args) == 0                           # informational: report only
    assert main([*args, "--strict"]) == 1            # strict: fail closed
    err = capsys.readouterr().err
    assert "zero memos scored" in err
