"""First live-model committee run (Phase 2 evals: 'are the memos actually good?').

Assembles REAL evidence from the DCP (vendor bars, indicators, the momentum-v1
gate verdict from the written report), runs the bull/bear debate, then the CIO
committee memo — all through the full cage: schema gates, grounding verifier,
budget breaker, audit chain. The memo lands in research.memos and appears on
the dashboard's Research page for the Principal's Phase-2 review.

Usage: ATLAS_MODEL_DEFAULT=claude-opus-4-8 python -m atlas.agents.live_run --symbol AVGO
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.agents.roles.cio import committee_memo
from atlas.agents.roles.debate import run_debate
from atlas.agents.runtime.runner import AgentRunFailed
from atlas.agents.runtime.registry import build_client, resolve_model
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import SystemClock
from atlas.core.db import session_scope
from atlas.dcp.indicators.core import rolling_return, sma

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs" / "reports" / "first-real-backtest-momentum-v1.md"


def build_evidence(s: Session, symbol: str) -> list[tuple[str, str]]:
    rows = s.execute(text(
        "SELECT pb.bar_date, pb.close FROM market.price_bars_daily pb "
        "JOIN market.instruments i ON i.id = pb.instrument_id "
        "WHERE i.symbol = :sym AND pb.source = 'EodhdAdapter' "
        "ORDER BY pb.bar_date DESC LIMIT 60"), {"sym": symbol}).all()
    if len(rows) < 51:
        raise LookupError(f"not enough real bars for {symbol} — run the backfill first")
    rows = list(reversed(rows))
    closes = [float(r.close) for r in rows]
    last_date = rows[-1].bar_date.isoformat()

    ev_bars = (f"{symbol} daily closes (EODHD vendor bars): latest close "
               f"{closes[-1]:.2f} on {last_date}, previous close {closes[-2]:.2f}, "
               f"20 sessions ago {closes[-21]:.2f}. Window: {len(closes)} sessions "
               f"ending {last_date}.")

    s20, s50 = sma(closes, 20)[-1], sma(closes, 50)[-1]
    r20 = rolling_return(closes, 20)[-1]
    assert s20 is not None and s50 is not None and r20 is not None
    ev_ind = (f"DCP indicators for {symbol} as of {last_date}: SMA20 {s20:.2f}, "
              f"SMA50 {s50:.2f}, 20-day return {r20 * 100:.2f} percent, last close "
              f"{closes[-1]:.2f}.")

    # The written gate-verdict artifact, quoted verbatim for the symbol's section
    # (checkpoint 3's report) — the strongest honest evidence we hold. Symbols
    # without a section get the honest family-level statement instead: a
    # digit-free fallback starves the debate of grounded numbers and guarantees
    # grounding kills (observed live on IBN).
    ev_quant = (f"No symbol-specific quant validation exists for {symbol}. "
                f"Family-level fact: momentum v1 was validated on real data for "
                f"SPY and AVGO only and FAILED every gate on both (null-model, "
                f"buy-and-hold, deflated Sharpe). No validated strategy covers "
                f"{symbol}; any momentum thesis on it is untested, not merely "
                f"unproven.")
    if REPORT.exists():
        m = re.search(rf"## {symbol} — (.*?)(?=\n## |\Z)", REPORT.read_text(), re.S)
        if m:
            ev_quant = (f"Quant validation report (momentum v1, real data, verbatim "
                        f"excerpt): {' '.join(m.group(0).split())[:1200]}")

    return [
        (f"dcp:bars:{symbol}:{last_date}", ev_bars),
        (f"dcp:indicators:{symbol}:{last_date}", ev_ind),
        (f"quant:report:momentum-v1:{symbol}", ev_quant),
    ]


def main() -> None:
    p = argparse.ArgumentParser(description="Live committee run (Phase 2 evals)")
    p.add_argument("--symbol", default="AVGO")
    p.add_argument("--question",
                   default="Given the quant gate verdict and current trend evidence, "
                           "what should the committee do with this name?")
    a = p.parse_args()

    print(f"models: debate={resolve_model('debate_bull')} cio={resolve_model('cio')}")
    with session_scope() as s:
        audit = PostgresAuditLog(s, SystemClock())
        try:
            evidence = build_evidence(s, a.symbol)
        except LookupError as e:
            raise SystemExit(str(e)) from None
        for ref, body in evidence:
            print(f"\nEVIDENCE [{ref}]\n  {body}")

        try:
            print("\nrunning bull/bear debate (4 calls)...")
            debate = run_debate(session=s, audit=audit,
                                client=build_client("debate_bull"),
                                symbol=a.symbol, evidence=evidence)
            print(f"  bull:  {'; '.join(debate.bull.strongest_points[:2])} ...")
            print(f"  bear:  {'; '.join(debate.bear.strongest_points[:2])} ...")

            print("\nrunning CIO committee memo...")
            memo = committee_memo(session=s, audit=audit, client=build_client("cio"),
                                  symbol=a.symbol, question=a.question,
                                  evidence=evidence, debate=debate)
        except AgentRunFailed as e:
            # fail-closed is a valid outcome; commit so the failed runs' cost
            # and audit trail persist (rollback would hide real spend)
            s.commit()
            raise SystemExit(f"CAGE HELD — run failed closed: {e}") from None

        cost = s.execute(text(
            "SELECT COALESCE(SUM(cost_usd),0), count(*) FROM research.agent_runs "
            "WHERE created_at::date = CURRENT_DATE")).one()

    print("\n" + "=" * 72)
    print(f"RECOMMENDATION: {memo.recommendation}   CONVICTION: {memo.conviction}")
    print(f"\nTHESIS\n  {memo.thesis}")
    print("\nKILL CRITERIA")
    for k in memo.kill_criteria:
        print(f"  - {k}")
    print(f"\nDISSENT\n  {memo.dissent}")
    print(f"\nDEBATE SUMMARY\n  {memo.debate_summary}")
    print(f"\nevidence_refs: {memo.evidence_refs}")
    print(f"today's LLM spend: ${float(cost[0]):.4f} across {cost[1]} run(s) "
          f"(breaker at $10.00)")
    print("memo persisted — see the dashboard Research page (localhost:8501)")


if __name__ == "__main__":
    main()
