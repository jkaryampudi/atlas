"""The research desk as a callable unit: one debate + committee memo per
candidate symbol, through the FULL cage (schema gates, grounding verifier,
$10/day budget breaker, audit chain), fail-soft per symbol.

This is what the daily cycle runs (atlas/ops/daily.py t7_desk): the agents'
job is to EMIT MEMOS — recommendations with kill criteria and dissent — onto
the console's Research page. Nothing here sizes, prices, or proposes trades:
the memo->proposal bridge is deliberately absent until the deterministic
stop-derivation policy is decided (CLAUDE.md invariant 2: agent numbers never
reach execution).

Fail-soft means HONEST: a symbol whose run the cage kills (grounding
violation, schema failure, budget breaker) is recorded as a cage hold in the
report — a held cage is the system working, never an exception that stops the
other symbols or the pipeline. Failed runs' cost still persists (the runner
commits agent_runs rows); the breaker counts them.

Usage (manual): python -m atlas.agents.desk --symbols SPY,AVGO
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.agents.live_run import build_evidence
from atlas.agents.roles.cio import committee_memo
from atlas.agents.roles.debate import run_debate
from atlas.agents.runtime.registry import build_client
from atlas.agents.runtime.runner import AgentRunFailed
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock

QUESTION = ("Given the quant gate verdict and current trend evidence, "
            "what should the committee do with this name?")


@dataclass(frozen=True)
class DeskMemo:
    symbol: str
    recommendation: str
    conviction: str


@dataclass(frozen=True)
class DeskReport:
    memos: tuple[DeskMemo, ...] = ()
    cage_holds: tuple[tuple[str, str], ...] = ()   # (symbol, reason)
    skipped: tuple[tuple[str, str], ...] = ()      # (symbol, why) — e.g. thin history
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


def run_desk(session: Session, clock: Clock, symbols: list[str]) -> DeskReport:
    """Debate + committee memo per symbol; every outcome recorded, none fatal."""
    audit = PostgresAuditLog(session, clock)
    memos: list[DeskMemo] = []
    holds: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    for symbol in symbols:
        try:
            evidence = build_evidence(session, symbol)
        except LookupError as e:
            skipped.append((symbol, str(e)))
            continue
        try:
            debate = run_debate(session=session, audit=audit,
                                client=build_client("debate_bull"),
                                symbol=symbol, evidence=evidence)
            memo = committee_memo(session=session, audit=audit,
                                  client=build_client("cio"), symbol=symbol,
                                  question=QUESTION, evidence=evidence,
                                  debate=debate)
            memos.append(DeskMemo(symbol=symbol,
                                  recommendation=memo.recommendation,
                                  conviction=memo.conviction))
        except AgentRunFailed as e:
            holds.append((symbol, str(e)[:200]))   # cage held — honest outcome
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
