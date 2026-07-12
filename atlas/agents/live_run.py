"""First live-model committee run (Phase 2 evals: 'are the memos actually good?').

Assembles REAL evidence from the DCP (vendor bars split-adjusted on read,
indicators, and the registry-driven quant validation record), runs the
bull/bear debate, then the CIO committee memo — all through the full cage:
schema gates, grounding verifier, budget breaker, audit chain. The memo lands
in research.memos and appears on the dashboard's Research page for the
Principal's Phase-2 review.

Usage: ATLAS_MODEL_DEFAULT=claude-opus-4-8 python -m atlas.agents.live_run --symbol AVGO
"""
from __future__ import annotations

import argparse
import hashlib
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.agents.roles.cio import committee_memo
from atlas.agents.roles.debate import run_debate
from atlas.agents.runtime.runner import PROMPTS, AgentRunFailed
from atlas.agents.runtime.registry import build_client, resolve_model
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import SystemClock
from atlas.core.db import session_scope
from atlas.dcp.backtest.quant_evidence import build_quant_evidence
from atlas.dcp.indicators.core import rolling_return, sma
from atlas.dcp.market_data.adjustment import adjust_for_splits
from atlas.dcp.market_data.desk_context import (extract_regime_evidence,
                                                extract_scanner_context)
from atlas.dcp.market_data.earnings import extract_earnings_evidence
from atlas.dcp.market_data.fundamentals import extract_fundamentals_evidence
from atlas.dcp.market_data.models import Bar, Split

QUESTION_TEMPLATE_REL_PATH = "question/default.md"


def load_default_question() -> tuple[str, str]:
    """(question_text, sha256-of-template-file) — desk-review 2026-07 item 10.

    The default Principal's question used to hide in an argparse default;
    prompts are code (CLAUDE.md invariant 5), and the question shapes every
    committee memo, so it lives in atlas/agents/prompts/question/ under the
    same review-and-hash discipline as every other template. The role
    templates' recorded prompt_template_hash covers constitution + role
    template only (the question rides in the memo context), so this file
    hash is printed with the run to pin the exact question text used.

    OPERATOR NOTE (duplication, deliberate): atlas/agents/desk.py loads the
    SAME template file with an identical 3-line loader (same raw-bytes hash
    convention). The loader cannot be shared from here without a circular
    import (desk.py imports build_evidence from this module); hoisting one
    shared loader into runtime/runner.py is a clean follow-up once both
    workstreams land. A unit test pins the two loaders equal meanwhile."""
    raw = (PROMPTS / QUESTION_TEMPLATE_REL_PATH).read_text()
    return raw.strip(), hashlib.sha256(raw.encode()).hexdigest()


def build_evidence(s: Session, symbol: str) -> list[tuple[str, str]]:
    rows = s.execute(text(
        "SELECT pb.bar_date, pb.close FROM market.price_bars_daily pb "
        "JOIN market.instruments i ON i.id = pb.instrument_id "
        "WHERE i.symbol = :sym AND pb.source = 'EodhdAdapter' "
        "ORDER BY pb.bar_date DESC LIMIT 60"), {"sym": symbol}).all()
    if len(rows) < 51:
        raise LookupError(f"not enough real bars for {symbol} — run the backfill first")
    rows = list(reversed(rows))
    last_date = rows[-1].bar_date.isoformat()

    # Split-adjust on read (desk-review 2026-07 item 2): vendor bars are stored
    # RAW (backfill convention), so a split inside the 60-session window would
    # fabricate a phantom move — a 10:1 split reads as a -90% "drop" — and the
    # cage would groundedly argue from it. Only splits known on or before the
    # last bar date apply (no look-ahead); closes-only path, so the degenerate
    # OHLC below only satisfies the Bar invariant and is never read back.
    splits = [Split(symbol=symbol, action_date=r.action_date,
                    ratio=Decimal(r.ratio))
              for r in s.execute(text(
                  "SELECT ca.action_date, ca.ratio FROM market.corporate_actions ca "
                  "JOIN market.instruments i ON i.id = ca.instrument_id "
                  "WHERE i.symbol = :sym AND ca.action_type = 'split' "
                  "  AND ca.action_date <= :d ORDER BY ca.action_date"),
                  {"sym": symbol, "d": rows[-1].bar_date}).all()]
    bars = [Bar(symbol=symbol, bar_date=r.bar_date, open=r.close, high=r.close,
                low=r.close, close=r.close, volume=0) for r in rows]
    closes = [float(b.close) for b in adjust_for_splits(bars, splits)]

    ev_bars = (f"{symbol} daily closes (EODHD vendor bars, split-adjusted): "
               f"latest close "
               f"{closes[-1]:.2f} on {last_date}, previous close {closes[-2]:.2f}, "
               f"20 sessions ago {closes[-21]:.2f}. Window: {len(closes)} sessions "
               f"ending {last_date}.")

    s20, s50 = sma(closes, 20)[-1], sma(closes, 50)[-1]
    r20 = rolling_return(closes, 20)[-1]
    assert s20 is not None and s50 is not None and r20 is not None
    ev_ind = (f"DCP indicators for {symbol} as of {last_date}: SMA20 {s20:.2f}, "
              f"SMA50 {s50:.2f}, 20-day return {r20 * 100:.2f} percent, last close "
              f"{closes[-1]:.2f}.")

    # Block 3 — the quant validation record, rendered deterministically from
    # quant.trial_registry + recorded gate verdicts (desk-review 2026-07 item
    # 1). Never a scraped report file: see dcp/backtest/quant_evidence.py for
    # the provenance and the reviewed suspension constants. The render carries
    # grounded numbers per family, so the debate is never starved of digits
    # (the digit-free-fallback grounding kills observed live on IBN).
    evidence = [
        (f"dcp:bars:{symbol}:{last_date}", ev_bars),
        (f"dcp:indicators:{symbol}:{last_date}", ev_ind),
        build_quant_evidence(s, symbol),
    ]

    # Blocks 4-7 share one contract: DCP-side extraction of numeric /
    # closed-vocabulary / ISO-date facts only (vendor free text is a
    # prompt-injection surface and never enters evidence), and None = no
    # record — the desk keeps the evidence set above rather than fabricating
    # a line. `on` is the symbol's last bar date, so every block is honest to
    # the same evidence date.
    on = rows[-1].bar_date
    # 4. fundamentals: whitelisted numeric facts from the latest snapshot
    # 5. earnings calendar (desk-review item 9): next/last report as ISO
    #    dates + session counts — ~1 in 3 memos straddles a print
    # 6. market regime (item 10a): SPY label from the deterministic P3
    #    classifier, closed vocabulary, never a warmup artifact
    # 7. scanner context (item 10b): WHY the scanner routed this name —
    #    score components from the scanner.completed audit payload, labelled
    #    attention-not-prediction; None on analyze-box runs and stale scans
    for block in (extract_fundamentals_evidence(s, symbol, on=on),
                  extract_earnings_evidence(s, symbol, on=on),
                  extract_regime_evidence(s, on=on),
                  extract_scanner_context(s, symbol, on=on)):
        if block is not None:
            evidence.append(block)
    return evidence


def main() -> None:
    p = argparse.ArgumentParser(description="Live committee run (Phase 2 evals)")
    p.add_argument("--symbol", default="AVGO")
    p.add_argument("--question", default=None,
                   help="override the Principal's standing question (default: the "
                        "hashed template atlas/agents/prompts/question/default.md)")
    a = p.parse_args()
    if a.question is None:
        a.question, q_hash = load_default_question()
        print(f"question template {QUESTION_TEMPLATE_REL_PATH} sha256 {q_hash[:12]}")

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
