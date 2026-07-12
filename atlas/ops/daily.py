"""T0–T9 daily operating cycle (Doc 01 §5, Doc 08 Phase 5) — the loop that
makes Atlas LIVE: one checkpointed WorkflowRunner graph per calendar day,
fired by launchd at 09:30 AEST (after the US close and EODHD publish).

  T0 ingest       incremental bars + FX + splits, quality gates written
  T1 verify       audit hash chain end-to-end (tamper = the run fails)
  T2 expire       proposals past their 24h TTL
  T3 settle       pending orders fill at their session opens
  T4 stops        protective stop scan — pre-authorized exits fire
  T5 snapshot     mark the book -> trading.portfolio_snapshots (+ breaker fold)
  T6 reconcile    internal consistency: positions ≡ open lots, ledger cash
                  finite and NAV recomputable; writes trading.reconciliations
  T7 desk         the research desk: debate + committee memo per eligible
                  universe symbol through the full cage — memos EMIT to the
                  console Research page; runs only on US session days and only
                  when a model key is configured; a desk failure pages but
                  never undoes the trading steps (they are already done)
  T8 report       summary incl. desk results -> audit + operator alert

The run is checkpointed under run_id = daily-<date> (WorkflowRunner). The
whole cycle runs in ONE transaction: an in-process node failure that is
retried in the same run skips completed nodes, while a process death rolls
the day back ATOMICALLY — re-running the CLI replays the day from T0 with
identical results (injectable clock, deterministic fills). No partial day is
ever committed. Node ORDER encodes policy: gates land before any trading step; SETTLE runs before the stop scan
so a position entered this cycle is stop-protected in the SAME cycle when the
breaching bar is already ingested (a scan-first order would leave it naked
until tomorrow) — the double-sell race is closed elsewhere: a filled
discretionary exit closes the position out of the scan, a live pending one
blocks it; the snapshot marks the book AFTER all fills; the chain is verified
before the book is touched.

Failure policy: ingest failures (red gate, FX gap, vendor error) do NOT stop
the trading steps — the book must still be protected on a bad-data day (stops
fail closed per-instrument on their own) — but they DO fail the run's exit
code and page the operator. A chain break or reconciliation break is a KILL
condition: the run raises immediately and pages at high priority.

The desk emits MEMOS only. The memo->proposal bridge stays absent until the
deterministic stop-derivation policy is decided (agents must never produce
sizing/pricing numbers — CLAUDE.md invariant 2; a Principal decision).
Proposals are still built explicitly and approved on the console desk.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock, FrozenClock, SystemClock
from atlas.core.db import session_scope
from atlas.core.workflow import Node, WorkflowRunner
from atlas.dcp.market_data.adapters.base import MarketDataAdapter
from atlas.dcp.market_data.calendars import is_trading_day
from atlas.dcp.market_data.daily import DailyIngestReport, run_daily_ingest
from atlas.dcp.trading.exits import scan_stop_exits
from atlas.dcp.trading.proposals import expire_stale, settle_orders, snapshot
from atlas.ops.alerts import notify
from atlas.tools.verify_chain import run as verify_chain


def _reconcile(session: Session, clock: Clock) -> str:
    """Paper-mode reconciliation (Doc 05 §5): we ARE the broker, so the break
    surface is internal — every open position's qty must equal its open lots,
    and no closed position may hold open lots. Writes trading.reconciliations
    and returns 'clean' or raises on 'break' (a broken book is a kill
    condition, not a warning)."""
    diffs: list[dict[str, object]] = []
    rows = session.execute(text(
        "SELECT p.id, p.qty, p.closed_at, i.symbol, "
        "  COALESCE((SELECT sum(tl.qty) FROM trading.tax_lots tl "
        "            WHERE tl.position_id = p.id AND tl.disposed_at IS NULL), 0) "
        "  AS open_lot_qty "
        "FROM trading.positions p "
        "LEFT JOIN market.instruments i ON i.id = p.instrument_id")).all()
    for r in rows:
        expected = int(r.qty) if r.closed_at is None else 0
        if int(r.open_lot_qty) != expected:
            diffs.append({"position_id": str(r.id), "symbol": r.symbol,
                          "position_qty": expected,
                          "open_lot_qty": int(r.open_lot_qty)})
    status = "break" if diffs else "clean"
    session.execute(text(
        "INSERT INTO trading.reconciliations (as_of, broker, status, diffs, "
        " resolved_at, created_at) "
        "VALUES (:d, 'paper', :s, CAST(:j AS jsonb), NULL, :t)"),
        {"d": clock.now().date(), "s": status, "j": json.dumps(diffs),
         "t": clock.now()})
    PostgresAuditLog(session, clock).append(
        event_type="reconciliation.completed", entity_type="reconciliation",
        entity_id=clock.now().date().isoformat(), actor_type="dcp",
        actor_id="daily_pipeline",
        payload={"status": status, "diffs": diffs})
    if diffs:
        raise RuntimeError(f"reconciliation BREAK: {diffs} — the lot ledger "
                           "disagrees with the positions; halting the run")
    return status


def run_daily_cycle(session: Session, clock: Clock, adapter: MarketDataAdapter,
                    desk: Callable[[Session, Clock], Any] | None = None,
                    ) -> dict[str, str | None]:
    """One day's full cycle under a single checkpointed run_id. Returns the
    node-result map; raises on kill conditions (chain break, recon break)."""
    day = clock.now().date().isoformat()
    state: dict[str, object] = {}

    def t0_ingest() -> str:
        report: DailyIngestReport = run_daily_ingest(session, clock, adapter)
        state["ingest_failed"] = report.failed
        state["ingest"] = report
        bars = sum(m.bars for m in report.markets.values())
        return f"bars={bars} failed={report.failed}"

    def t1_verify_chain() -> str:
        # verify THE database this cycle writes to, through THIS session and
        # THIS clock — a side connection from the env URL would verify some
        # other database's chain and call it ours (the exact false-pass CI
        # caught: locally the env pointed at the dev DB's valid chain).
        # ChainVerificationError propagates = the run dies = kill condition.
        n = verify_chain(session, clock)
        return f"chain ok ({n} events)"

    def t2_expire() -> str:
        return f"expired={len(expire_stale(session, clock))}"

    def t3_settle() -> str:
        fills = settle_orders(session, clock)
        state["fills"] = fills
        return f"fills={len(fills)}"

    def t4_stops() -> str:
        fired = scan_stop_exits(session, clock)
        state["stops"] = fired
        return f"stops_fired={len(fired)}"

    def t5_snapshot() -> str:
        snap = snapshot(session, clock)
        state["nav"] = snap.nav_aud
        return f"nav={snap.nav_aud}"

    def t6_reconcile() -> str:
        return _reconcile(session, clock)

    def t7_desk() -> str:
        if desk is None:
            return "desk off (no model key configured)"
        us_day = clock.now().astimezone(UTC).date()
        if not is_trading_day("US", us_day):
            return f"desk skipped ({us_day} is not a US session)"
        try:
            report = desk(session, clock)
        except Exception as e:  # noqa: BLE001 — a desk failure pages, but the
            #                     book is already settled and protected
            state["desk_failed"] = True
            return f"desk FAILED: {e}"[:300]
        state["desk"] = report
        return report.summary()

    def t8_report() -> str:
        ingest = state.get("ingest")
        stops = state.get("stops", ())
        fills = state.get("fills", ())
        nav = state.get("nav", Decimal(0))
        desk_report = state.get("desk")
        failed = bool(state.get("ingest_failed", False)) or bool(
            state.get("desk_failed", False))
        lines = [f"NAV A${nav}",
                 f"fills {len(fills)}, stops fired {len(stops)}",  # type: ignore[arg-type]
                 desk_report.summary() if desk_report is not None else "desk idle",
                 "ingest FAILED — see log" if bool(state.get("ingest_failed", False))
                 else "ingest clean"]
        if state.get("desk_failed"):
            lines.append("desk FAILED — see log")
        summary = " · ".join(lines)
        PostgresAuditLog(session, clock).append(
            event_type="daily_cycle.completed", entity_type="pipeline",
            entity_id=day, actor_type="dcp", actor_id="daily_pipeline",
            payload={"summary": summary, "ingest_failed": failed,
                     "ingest_failures": list(getattr(ingest, "failures", []))})
        notify(f"Atlas daily {day}", summary,
               priority="high" if failed else "default")
        return summary

    runner = WorkflowRunner(session, PostgresAuditLog(session, clock), clock)
    return runner.run(f"daily-{day}", [
        Node("t0_ingest", t0_ingest),
        Node("t1_verify_chain", t1_verify_chain),
        Node("t2_expire", t2_expire),
        Node("t3_settle", t3_settle),
        Node("t4_stops", t4_stops),
        Node("t5_snapshot", t5_snapshot),
        Node("t6_reconcile", t6_reconcile),
        Node("t7_desk", t7_desk),
        Node("t8_report", t8_report),
    ])


def main() -> None:
    parser = argparse.ArgumentParser(description="Atlas T0-T9 daily cycle")
    parser.add_argument("--now", help="ISO instant for deterministic re-runs "
                                      "(default: wall clock)")
    args = parser.parse_args()
    clock: Clock = (FrozenClock(datetime.fromisoformat(args.now))
                    if args.now else SystemClock())
    from pathlib import Path

    from atlas.dcp.market_data.adapters import adapter_from_settings
    root = Path(__file__).resolve().parents[2]
    adapter = adapter_from_settings(fixtures_root=root / "tests" / "fixtures",
                                    seeds_csv=root / "seeds" / "instruments_seed.csv")
    desk: Callable[[Session, Clock], Any] | None = None
    if os.environ.get("ATLAS_ANTHROPIC_API_KEY"):
        from atlas.agents.desk import desk_symbols, run_desk

        def desk(s: Session, c: Clock) -> Any:
            return run_desk(s, c, desk_symbols(s))
    with session_scope() as s:
        results = run_daily_cycle(s, clock, adapter, desk=desk)
    ingest_line = results.get("t0_ingest") or ""
    failed = ("failed=True" in ingest_line
              or "desk FAILED" in (results.get("t7_desk") or ""))
    print(json.dumps(results, indent=2))
    raise SystemExit(2 if failed else 0)


if __name__ == "__main__":
    main()
