"""Golden pins for the registry-driven quant evidence render (desk-review
2026-07 item 1; atlas/dcp/backtest/quant_evidence.py).

The render is the desk's block-3 evidence: byte-exact goldens here mean a
wording or number-format change is a reviewed diff, exactly like a prompt
template. The suspension constant is pinned verbatim — removing or editing it
requires the registered total-return re-score's verdict and a deliberate
change to this file.
"""
from __future__ import annotations

from datetime import date

from atlas.agents.runtime.grounding import corpus_numeric_tokens, numeric_tokens
from atlas.dcp.backtest.quant_evidence import (
    SUSPENSIONS,
    FamilyRecord,
    GateRun,
    QuantRecord,
    render_quant_evidence,
)

G_1Y_AVGO = GateRun(
    family="momentum", scope="AVGO", passed=False,
    window="2025-07-11..2026-07-10", decision_grade=False, years=0,
    dsr=0.2574519871016028, null_p=0.059, wf_positive_folds=2,
    universe=None, caveat=None,
    reasons=("null-model: p=0.059 > 0.05 (random entries do as well)",),
    recorded=date(2026, 7, 11))

G_16Y_SPY = GateRun(
    family="momentum", scope="SPY", passed=False,
    window="2010-01-04..2026-07-10", decision_grade=True, years=16,
    dsr=0.18183918826328527, null_p=0.923, wf_positive_folds=2,
    universe=None, caveat=None,
    reasons=("null-model: p=0.923 > 0.05 (random entries do as well)",
             "does not beat buy-and-hold (7.8% <= 544.2%)",
             "deflated Sharpe 0.18 < 0.9 at n_trials=6"),
    recorded=date(2026, 7, 12))

G_PIT = GateRun(
    family="xsmom-pit", scope="portfolio", passed=True,
    window="2010-01-04..2026-07-10", decision_grade=True, years=16,
    dsr=0.9976925177512832, null_p=0.0, wf_positive_folds=4,
    universe="point-in-time GSPC.INDX",
    caveat=("point-in-time membership INCLUDING delisted names — the "
            "definitive test of the S&P-100/ETF conditional chain"),
    reasons=(), recorded=date(2026, 7, 12))

RECORD = QuantRecord(
    families=(
        FamilyRecord(family="momentum", n_trials=7,
                     latest_registered=date(2026, 7, 12),
                     gates=(G_1Y_AVGO, G_16Y_SPY)),
        FamilyRecord(family="xsmom-pit", n_trials=1,
                     latest_registered=date(2026, 7, 12),
                     gates=(G_PIT,)),
    ),
    approved_families=(),
    as_of=date(2026, 7, 12))

GOLDEN = (
    "Quant validation record for IBN (deterministic DCP render v1 from "
    "quant.trial_registry and recorded gate verdicts; record as of "
    "2026-07-12). Per-family verdicts, key numbers and dates quoted from the "
    "recorded record:\n"
    "- momentum: gate FAIL. 2 gate run(s) recorded (scopes: AVGO, SPY), 0 "
    "passed; 7 trial(s) registered. Headline run momentum/SPY, window "
    "2010-01-04..2026-07-10 (decision-grade, 16 years), recorded 2026-07-12: "
    "DSR 0.182, null-model p 0.923, walk-forward positive folds 2. Gate "
    "reasons verbatim: null-model: p=0.923 > 0.05 (random entries do as "
    "well); does not beat buy-and-hold (7.8% <= 544.2%); deflated Sharpe "
    "0.18 < 0.9 at n_trials=6.\n"
    "- xsmom-pit: gate PASS recorded 2026-07-12 (point-in-time GSPC.INDX) — "
    "superseded by the total-return re-score xsmom-pit-tr (both TR runs "
    "PASS, recorded 2026-07-13); NOT approved for trading. 1 gate run(s) recorded (scopes: portfolio), 1 "
    "passed; 1 trial(s) registered. Window 2010-01-04..2026-07-10 "
    "(decision-grade, 16 years): DSR 0.998, null-model p 0.000, walk-forward "
    "positive folds 4. Recorded caveat verbatim: point-in-time membership "
    "INCLUDING delisted names — the definitive test of the S&P-100/ETF "
    "conditional chain.\n"
    "Approval state: quant.strategies holds no strategy in state "
    "approved/live/paper — NO strategy is approved for trading. No recorded "
    "gate run tests IBN directly; the verdicts above are family-level. Any "
    "thesis on IBN is untested, not merely unproven.")


def test_render_golden_pin_full_record():
    assert render_quant_evidence(RECORD, "IBN") == GOLDEN


def test_suspension_constant_pinned_verbatim():
    """A reviewed governance constant: this exact text is what the desk reads.
    Removing or editing it requires the registered TR re-score's verdict."""
    assert dict(SUSPENSIONS) == {
        "xsmom-pit": ("superseded by the total-return re-score xsmom-pit-tr "
                      "(both TR runs PASS, recorded 2026-07-13); NOT approved "
                      "for trading")}


def test_symbol_with_recorded_runs_gets_the_scope_tally():
    body = render_quant_evidence(RECORD, "SPY")
    assert body.endswith(
        "Approval state: quant.strategies holds no strategy in state "
        "approved/live/paper — NO strategy is approved for trading. SPY "
        "appears as a tested scope in 1 recorded gate run(s) (momentum): "
        "0 passed.")


def test_empty_registry_renders_the_honest_fallback():
    empty = QuantRecord(families=(), approved_families=(), as_of=None)
    assert render_quant_evidence(empty, "ZZZ") == (
        "Quant validation record (deterministic DCP render v1): "
        "quant.trial_registry holds no registered trials — the fund has no "
        "recorded backtest evidence for any strategy family. Approval state: "
        "quant.strategies holds no strategy in state approved/live/paper — "
        "NO strategy is approved for trading. Any thesis on ZZZ is untested, "
        "not merely unproven.")


def test_family_without_gate_verdict_renders_unvalidated():
    rec = QuantRecord(
        families=(FamilyRecord(family="meanrev", n_trials=3,
                               latest_registered=date(2026, 7, 12), gates=()),),
        approved_families=(), as_of=date(2026, 7, 12))
    assert ("- meanrev: no recorded gate verdict; 3 trial(s) registered "
            "(latest 2026-07-12). Unvalidated.") in render_quant_evidence(rec, "SPY")


def test_every_key_number_grounds_under_the_token_boundary_verifier():
    """The block is evidence: a memo quoting its numbers must ground. Every
    digit the render emits must therefore be a STANDALONE corpus token under
    the token-boundary tokenizer (no '16y'-style fused tokens)."""
    tokens = corpus_numeric_tokens(GOLDEN)
    for cited in ("0.182", "0.923", "0.998", "0.000", "7.8", "544.2", "0.18",
                  "0.9", "6", "16", "2", "4", "1", "7", "0.05"):
        assert cited in tokens, cited
    # and a plausible memo sentence built from the block grounds clean
    memo_sentence = ("The xsmom-pit gate passed with DSR 0.998 and null p "
                     "0.000 but momentum failed with DSR 0.182.")
    assert set(numeric_tokens(memo_sentence)) <= tokens
