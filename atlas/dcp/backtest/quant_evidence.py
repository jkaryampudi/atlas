"""Registry-driven quant evidence for the research desk (desk-review 2026-07 #1).

Block 3 of the desk's evidence set used to be regex-scraped from a hardcoded
markdown report — the desk's only unpinned, mutable evidence source — and by
2026-07 it told every model "no validated strategy exists" while the fund's
own signed xsmom-pit report recorded a decision-grade PASS. This module
replaces it with deterministic DCP output derived from DATA:

- quant.trial_registry     — every registered trial (family, count, recorded
  dates; ADR-0002 #1);
- audit.decision_events    — the recorded gate verdicts (event_type
  'quant.backtest.completed'; append-only, hash-chained), joined to the
  registry via payload->>'trial_id' so each verdict carries the registry's
  recorded date;
- quant.strategies         — the approval state (approved/live/paper rows).

Documented resolutions (v1, deliberate):

- SUSPENSIONS encodes governance facts the database does not yet model (the
  xsmom-pit total-return suspension, board memo 2026-07-13 item 1). It is a
  REVIEWED constant: living in code makes it diff-reviewed and hash-covered
  like a prompt template; removing an entry requires the registered re-score's
  verdict recorded verbatim. It is never a scraped report file.
- fxlab-* families are excluded: the FX lab is a sealed plane (ADR-0008,
  migration 0014) and its verdicts never reach the equities desk.
- NO per-symbol PIT-universe / winner-decile applicability line: point-in-time
  membership lives in the sealed `validation` schema (migration 0015 —
  "nothing in the reasoning plane may see it"), and decile ranks would require
  running the recipe. The block instead reports which recorded gate runs
  tested the symbol directly (cheap — it is the verdict's scope) and otherwise
  stays family-level.
- Decision-grade is derived exactly as the runners derive it (ADR-0004
  condition: recorded window spans >= 3650 days); an unparseable window fails
  closed to not-decision-grade.
- Numbers are rendered in fixed formats producing standalone tokens
  ("16 years", never "16y") so memos citing the block ground verbatim under
  the token-boundary grounding verifier.
- quant.strategies rows in states short of approved/live/paper (draft,
  backtested, validated) are deliberately not rendered: gate verdicts above
  carry the evidence; only the approval boundary is a desk-relevant fact.

Two-plane wall: pure DCP — recorded tables in, text out; imports no agent
code. live_run/desk import THIS module (agents -> dcp is the legal direction).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

RENDER_VERSION = "v1"
DECISION_GRADE_MIN_DAYS = 3650          # ADR-0004 full-history condition
EXCLUDED_FAMILY_PREFIXES: tuple[str, ...] = ("fxlab-",)   # sealed lab, ADR-0008
APPROVED_STATES: tuple[str, ...] = ("approved", "live", "paper")

# Reviewed governance constants (see module docstring). Keyed by family.
SUSPENSIONS: Mapping[str, str] = {
    # Board memo 2026-07-13 item 1 found the original PASS scored price-return
    # vs price-return where ADR-0009 requires SPY TOTAL return. The registered
    # re-score (xsmom-pit-tr and the 2016 kill test, both PASS, recorded
    # 2026-07-13) resolved the question: this family's verdict is SUPERSEDED
    # by xsmom-pit-tr, which carries the honest TR-vs-TR result. The endpoint
    # concentration exhibits live in the TR report. Changing this line again
    # requires a recorded verdict or a Principal decision, never convenience.
    "xsmom-pit": ("superseded by the total-return re-score xsmom-pit-tr "
                  "(both TR runs PASS, recorded 2026-07-13); NOT approved "
                  "for trading"),
}

_TRIALS_SQL = """
SELECT strategy_family AS family, count(*) AS n_trials,
       max(created_at)::date AS latest
FROM quant.trial_registry
GROUP BY strategy_family
ORDER BY strategy_family
"""

_GATES_SQL = """
SELECT tr.strategy_family AS family, e.entity_id AS entity_id,
       tr.created_at::date AS recorded, e.payload AS payload
FROM audit.decision_events e
JOIN quant.trial_registry tr ON tr.id::text = e.payload->>'trial_id'
WHERE e.event_type = 'quant.backtest.completed'
ORDER BY tr.created_at, e.entity_id
"""

_APPROVED_SQL = """
SELECT family, count(*) AS n FROM quant.strategies
WHERE state IN ('approved', 'live', 'paper')
GROUP BY family ORDER BY family
"""


@dataclass(frozen=True)
class GateRun:
    """One recorded gate verdict, joined to its registered trial."""
    family: str
    scope: str                      # entity_id suffix: symbol or 'portfolio'
    passed: bool
    window: str
    decision_grade: bool
    years: int | None               # floor of the window span, when parseable
    dsr: float | None
    null_p: float | None
    wf_positive_folds: int | None
    universe: str | None
    caveat: str | None              # recorded survivorship caveat/note, verbatim
    reasons: tuple[str, ...]        # recorded gate reasons, verbatim
    recorded: date                  # trial_registry.created_at::date


@dataclass(frozen=True)
class FamilyRecord:
    family: str
    n_trials: int
    latest_registered: date
    gates: tuple[GateRun, ...]      # sorted (decision_grade, recorded, scope)

    @property
    def headline(self) -> GateRun | None:
        """The gate run the family line quotes: decision-grade wins, then the
        latest recorded date, then scope (deterministic total order)."""
        return self.gates[-1] if self.gates else None


@dataclass(frozen=True)
class QuantRecord:
    """Everything the render needs, loaded in one deterministic pass."""
    families: tuple[FamilyRecord, ...]          # family-ordered
    approved_families: tuple[str, ...]          # states approved/live/paper
    as_of: date | None                          # newest recorded date, if any


def _window_span_days(window: str) -> int | None:
    try:
        start_s, end_s = window.split("..")
        return (date.fromisoformat(end_s) - date.fromisoformat(start_s)).days
    except (ValueError, AttributeError):
        return None


def _gate_run(family: str, entity_id: str, recorded: date,
              payload: dict[str, Any]) -> GateRun | None:
    if not isinstance(payload.get("gate_passed"), bool):
        return None                 # not a verdict payload — fail closed
    window = str(payload.get("window", ""))
    span = _window_span_days(window)
    caveat = payload.get("survivorship_caveat") or payload.get("survivorship_note")
    return GateRun(
        family=family,
        scope=entity_id.rsplit("/", 1)[-1],
        passed=bool(payload["gate_passed"]),
        window=window,
        decision_grade=span is not None and span >= DECISION_GRADE_MIN_DAYS,
        years=None if span is None else span // 365,
        dsr=None if payload.get("dsr") is None else float(payload["dsr"]),
        null_p=None if payload.get("null_p") is None else float(payload["null_p"]),
        wf_positive_folds=(None if payload.get("wf_positive_folds") is None
                           else int(payload["wf_positive_folds"])),
        universe=payload.get("universe"),
        caveat=caveat,
        reasons=tuple(str(r) for r in payload.get("gate_reasons", [])),
        recorded=recorded)


def load_quant_record(session: Session) -> QuantRecord:
    """Deterministic load: same tables, same record, byte for byte."""
    trials = [r for r in session.execute(text(_TRIALS_SQL)).all()
              if not r.family.startswith(EXCLUDED_FAMILY_PREFIXES)]
    gates: dict[str, list[GateRun]] = {}
    for r in session.execute(text(_GATES_SQL)).all():
        if r.family.startswith(EXCLUDED_FAMILY_PREFIXES):
            continue
        run = _gate_run(r.family, r.entity_id, r.recorded, r.payload)
        if run is not None:
            gates.setdefault(r.family, []).append(run)
    families = tuple(
        FamilyRecord(family=t.family, n_trials=int(t.n_trials),
                     latest_registered=t.latest,
                     gates=tuple(sorted(gates.get(t.family, []),
                                        key=lambda g: (g.decision_grade,
                                                       g.recorded, g.scope))))
        for t in trials)
    approved = tuple(r.family for r in session.execute(text(_APPROVED_SQL)).all())
    dates = [f.latest_registered for f in families] + [
        g.recorded for f in families for g in f.gates]
    return QuantRecord(families=families, approved_families=approved,
                       as_of=max(dates) if dates else None)


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _numbers_clause(head: GateRun) -> str:
    wf = "n/a" if head.wf_positive_folds is None else str(head.wf_positive_folds)
    return (f"DSR {_fmt(head.dsr)}, null-model p {_fmt(head.null_p)}, "
            f"walk-forward positive folds {wf}")


def _grade_clause(head: GateRun) -> str:
    if head.decision_grade:
        years = "" if head.years is None else f", {head.years} years"
        return f"(decision-grade{years})"
    return "(not decision-grade)"


def _family_line(fam: FamilyRecord, approved_families: tuple[str, ...]) -> str:
    head = fam.headline
    trials = f"{fam.n_trials} trial(s) registered"
    if head is None:
        return (f"- {fam.family}: no recorded gate verdict; {trials} "
                f"(latest {fam.latest_registered.isoformat()}). Unvalidated.")
    n_pass = sum(1 for g in fam.gates if g.passed)
    tally = (f"{len(fam.gates)} gate run(s) recorded "
             f"(scopes: {', '.join(sorted({g.scope for g in fam.gates}))}), "
             f"{n_pass} passed; {trials}.")
    if head.passed:
        status = SUSPENSIONS.get(fam.family)
        if status is None and fam.family not in approved_families:
            status = "NOT approved for trading"
        where = head.universe or head.scope
        line = (f"- {fam.family}: gate PASS recorded "
                f"{head.recorded.isoformat()} ({where})"
                + (f" — {status}." if status else ".")
                + f" {tally} Window {head.window} {_grade_clause(head)}: "
                + f"{_numbers_clause(head)}.")
    else:
        line = (f"- {fam.family}: gate FAIL. {tally} Headline run "
                f"{head.family}/{head.scope}, window {head.window} "
                f"{_grade_clause(head)}, recorded {head.recorded.isoformat()}: "
                f"{_numbers_clause(head)}.")
        if head.reasons:
            line += f" Gate reasons verbatim: {'; '.join(head.reasons)}."
    if head.caveat:
        line += f" Recorded caveat verbatim: {head.caveat}."
    return line


def _approval_clause(record: QuantRecord) -> str:
    if not record.approved_families:
        return ("Approval state: quant.strategies holds no strategy in state "
                "approved/live/paper — NO strategy is approved for trading.")
    fams = ", ".join(record.approved_families)
    return (f"Approval state: quant.strategies holds approved/live/paper "
            f"rows for: {fams}.")


def _symbol_clause(record: QuantRecord, symbol: str) -> str:
    runs = [g for f in record.families for g in f.gates if g.scope == symbol]
    if not runs:
        return (f"No recorded gate run tests {symbol} directly; the verdicts "
                f"above are family-level. Any thesis on {symbol} is untested, "
                f"not merely unproven.")
    fams = ", ".join(sorted({g.family for g in runs}))
    n_pass = sum(1 for g in runs if g.passed)
    return (f"{symbol} appears as a tested scope in {len(runs)} recorded gate "
            f"run(s) ({fams}): {n_pass} passed.")


def render_quant_evidence(record: QuantRecord, symbol: str) -> str:
    """Pure, deterministic render — golden-pinned in the tests. Same record +
    same symbol => the same text, byte for byte."""
    if not record.families:
        return (f"Quant validation record (deterministic DCP render "
                f"{RENDER_VERSION}): quant.trial_registry holds no registered "
                f"trials — the fund has no recorded backtest evidence for any "
                f"strategy family. {_approval_clause(record)} Any thesis on "
                f"{symbol} is untested, not merely unproven.")
    as_of = record.as_of.isoformat() if record.as_of else "unknown"
    lines = [(f"Quant validation record for {symbol} (deterministic DCP render "
              f"{RENDER_VERSION} from quant.trial_registry and recorded gate "
              f"verdicts; record as of {as_of}). Per-family verdicts, key "
              f"numbers and dates quoted from the recorded record:")]
    lines += [_family_line(f, record.approved_families) for f in record.families]
    lines.append(f"{_approval_clause(record)} {_symbol_clause(record, symbol)}")
    return "\n".join(lines)


def build_quant_evidence(session: Session, symbol: str) -> tuple[str, str]:
    """The desk's block-3 evidence tuple: (version-pinned ref, rendered body)."""
    body = render_quant_evidence(load_quant_record(session), symbol)
    return f"dcp:quant:verdicts:{RENDER_VERSION}:{symbol}", body
