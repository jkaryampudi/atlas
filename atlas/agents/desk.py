"""The research desk as a callable unit: one debate + committee memo per
candidate symbol, through the FULL cage (schema gates, grounding verifier,
$10/day budget breaker, audit chain), fail-soft per symbol.

This is what the daily cycle runs (atlas/ops/daily.py t7_desk): the agents'
job is to EMIT MEMOS — recommendations with kill criteria and dissent — onto
the console's Research page. Nothing here sizes, prices, or proposes trades:
the memo->proposal bridge is deliberately absent until the deterministic
stop-derivation policy is decided (CLAUDE.md invariant 2: agent numbers never
reach execution).

Fail-soft means HONEST, and outcomes are typed (desk-review 2026-07 item 6):
- CAGE HOLD — schema/grounding kill or the budget breaker: the system working,
  recorded per symbol, never an exception that stops the other symbols.
  Failed runs' cost still persists (the runner commits agent_runs rows); the
  breaker counts them. A tripped breaker additionally HALTS the remaining
  shortlist (each further attempt would still hit the vendor before the check
  — real spend a tripped breaker forbids) and records those symbols as
  not-attempted holds.
- TRANSIENT SKIP — HTTP 429/5xx/timeout that survived the runner's bounded
  backoff: vendor plumbing, not a verdict on the symbol; recorded in
  `skipped` with a 'transient:' reason and the shortlist continues.

Usage (manual): python -m atlas.agents.desk --symbols SPY,AVGO
"""
from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.agents.live_run import build_evidence
from atlas.agents.roles.cio import committee_memo
from atlas.agents.roles.debate import run_debate
from atlas.agents.roles.specialists import has_signal_block, run_specialists
from atlas.agents.runtime.budget import BudgetExhausted
from atlas.agents.runtime.registry import build_client
from atlas.agents.runtime.runner import (
    PROMPTS,
    AgentRunFailed,
    TransientLlmFailure,
    budget_surface,
    current_budget_surface,
)
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock

# The Principal's standing question lives in the prompt store (desk-review
# 2026-07 item 10): prompts are code (CLAUDE.md invariant 5), so changing the
# question is a reviewed diff and tests/unit/test_desk_question.py golden-pins
# the file hash. live_run.py's copy of this string should converge on the same
# template file.
QUESTION_TEMPLATE_REL_PATH = "question/default.md"


def load_question() -> tuple[str, str]:
    """(question_text, sha256-of-file) from the hashed prompt-template store."""
    raw = (PROMPTS / QUESTION_TEMPLATE_REL_PATH).read_text()
    return raw.strip(), hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class DeskMemo:
    symbol: str
    recommendation: str
    conviction: str


@dataclass(frozen=True)
class DeskReport:
    memos: tuple[DeskMemo, ...] = ()
    cage_holds: tuple[tuple[str, str], ...] = ()   # (symbol, reason)
    skipped: tuple[tuple[str, str], ...] = ()      # (symbol, why) — thin history,
    #                                                or 'transient: ...' LLM-transport skips
    cost_usd_today: float = 0.0

    def summary(self) -> str:
        recs = ", ".join(f"{m.symbol}:{m.recommendation}" for m in self.memos) or "none"
        return (f"memos {len(self.memos)} ({recs}) · cage holds "
                f"{len(self.cage_holds)} · spend today ${self.cost_usd_today:.2f}")


def desk_symbols(session: Session, *, min_bars: int = 51) -> list[str]:
    """Active universe symbols with enough vendor history to build evidence."""
    rows = session.execute(text(
        "SELECT i.symbol, count(pb.*) AS n FROM market.instruments i "
        "LEFT JOIN market.price_bars_daily pb ON pb.instrument_id = i.id "
        "  AND pb.source = 'EodhdAdapter' "
        "WHERE i.is_active GROUP BY i.symbol ORDER BY i.symbol")).all()
    return [r.symbol for r in rows if int(r.n) >= min_bars]


def run_desk(session: Session, clock: Clock, symbols: list[str],
             source: str | None = None) -> DeskReport:
    """Debate + committee memo per symbol; every outcome recorded, none fatal.

    `source` is the optional external-origin tag for on-demand analyses
    (ANALYZE-ANY-TICKER; e.g. 'investing.com'), threaded verbatim to
    committee_memo where it is persisted with the memo row — it never enters
    any prompt (see cio.py). None = the desk's own work (nightly cycle).

    Budget surface: unless a caller already bound one (analyze.py binds
    'analyze'), every run here counts against the NIGHTLY sub-cap
    (ATLAS_BUDGET_NIGHTLY) inside the global breaker — see runner.py for the
    watermark semantics and precedence (global always wins)."""
    audit = PostgresAuditLog(session, clock)
    question, _ = load_question()
    memos: list[DeskMemo] = []
    holds: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    with budget_surface(current_budget_surface() or "nightly"):
        for i, symbol in enumerate(symbols):
            try:
                evidence = build_evidence(session, symbol)
            except LookupError as e:
                skipped.append((symbol, str(e)))
                continue
            try:
                debate = run_debate(session=session, audit=audit,
                                    symbol=symbol, evidence=evidence)
                # Specialist panel (ADR-0011 step 2): AFTER the debate, BEFORE
                # the CIO — and ONLY for signal-lane names (evidence carries a
                # dcp:signal: block, i.e. names that could become BUYs).
                # Scanner-only names skip the panel to protect the budget.
                #
                # BUDGET ARITHMETIC (honest, reviewed — do NOT raise any cap):
                # the desk spends ~$0.23/name across ~5 calls today, so one
                # call is ~$0.03-0.05; the panel adds 3 calls/signal-name at
                # max_tokens=1200 (< the debate's 2500) ≈ +$0.10-0.15/name.
                # The shortlist is top-5 momentum + top-5 PEAD (+ scanner 5),
                # so ≈10 signal-lane names/night ≈ +$1.2 on top of ~$2.5 —
                # ~$3.7, inside the $6 NIGHTLY sub-cap (ATLAS_BUDGET_NIGHTLY)
                # with the global $10 breaker above it. The worst case is
                # already handled: BudgetExhausted from any specialist call
                # propagates here (specialists are fail-soft for cage and
                # transport failures ONLY, never for the breaker) and takes
                # the hold-and-halt path below.
                panel = (run_specialists(session=session, audit=audit,
                                         symbol=symbol, evidence=evidence)
                         if has_signal_block(evidence) else None)
                memo = committee_memo(session=session, audit=audit,
                                      client=build_client("cio"), symbol=symbol,
                                      question=question, evidence=evidence,
                                      debate=debate, specialists=panel,
                                      source=source)
                memos.append(DeskMemo(symbol=symbol,
                                      recommendation=memo.recommendation,
                                      conviction=memo.conviction))
            except AgentRunFailed as e:
                holds.append((symbol, str(e)[:200]))   # cage held — honest outcome
            except TransientLlmFailure as e:
                # vendor plumbing, not a verdict: this symbol only; continue
                skipped.append((symbol, f"transient: {str(e)[:180]}"))
            except BudgetExhausted as e:
                # the breaker (global or surface sub-cap) is terminal for this
                # desk run: hold the symbol, halt the shortlist — attempting
                # the rest would still spend at the vendor before the check
                holds.append((symbol, f"budget: {str(e)[:180]}"))
                holds.extend((s, "budget exhausted — not attempted")
                             for s in symbols[i + 1:])
                break
    cost = session.execute(text(
        "SELECT COALESCE(SUM(cost_usd),0) FROM research.agent_runs "
        "WHERE created_at::date = :d"), {"d": clock.now().date()}).scalar()
    return DeskReport(memos=tuple(memos), cage_holds=tuple(holds),
                      skipped=tuple(skipped), cost_usd_today=float(cost or 0))


def main() -> None:
    from atlas.core.clock import SystemClock
    from atlas.core.db import session_scope

    p = argparse.ArgumentParser(description="Run the research desk (debate + CIO memo)")
    p.add_argument("--symbols", help="comma-separated; default: full eligible universe")
    a = p.parse_args()
    with session_scope() as s:
        symbols = (a.symbols.split(",") if a.symbols else desk_symbols(s))
        print(f"desk candidates: {symbols}")
        report = run_desk(s, SystemClock(), symbols)
    print(report.summary())
    for sym, why in report.skipped:
        print(f"  skipped {sym}: {why}")
    for sym, why in report.cage_holds:
        print(f"  CAGE HELD {sym}: {why}")


if __name__ == "__main__":
    main()
