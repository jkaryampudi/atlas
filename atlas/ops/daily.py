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
  T7 desk         SCAN then DESK — one attention+analysis stage. The
                  deterministic scanner (ADR-0007, atlas/dcp/scanner/v1.py —
                  attention, not alpha) sweeps the FULL active universe for
                  free and only its shortlist (top-N by attention score, plus
                  held/in-flight names) reaches the LLM debate + committee
                  memo cage — memos EMIT to the console Research page; runs
                  only on US session days and only when a model key is
                  configured; a desk failure pages but never undoes the
                  trading steps (they are already done). If SCANNING breaks,
                  the desk falls back to the full eligible universe
                  (fail-soft: the desk must not go blind because ranking
                  broke) and the run pages like a desk failure
  T8 bridge       memo->proposal bridge (ADR-0006): fresh non-shadow committee
                  BUY memos become sized, L1-L11-checked proposals in the
                  console approval queue; every price derived from vendor bars
                  alone (CLAUDE.md invariant 2). Runs desk or no desk — a
                  manually-run desk's memos must still bridge next cycle; a
                  bridge failure pages exactly like a desk failure but never
                  undoes the prior steps
  T9 report       summary incl. desk + bridge results -> audit + operator alert

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

The desk emits MEMOS only; T8 (atlas.dcp.trading.bridge) is the sole path
from memo to proposal, under the signed ADR-0006 stop-derivation policy —
agents never produce sizing/pricing numbers (CLAUDE.md invariant 2), the DCP
derives entry/stop/target from vendor bars and the risk engine sizes. Human
approval on the console desk remains the only way a proposal becomes an order.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
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
from atlas.dcp.scanner.v1 import scan
from atlas.dcp.scorecard import compute_memo_outcomes
from atlas.dcp.trading.bridge import bridge_memos
from atlas.dcp.trading.exits import scan_stop_exits
from atlas.dcp.trading.proposals import expire_stale, settle_orders, snapshot
from atlas.ops.alerts import notify
from atlas.tools.verify_chain import run as verify_chain


def _emit(node: str, status: str, result: str | None = None) -> None:
    """One machine-readable progress line per node transition."""
    print("@@CYCLE " + json.dumps(
        {"node": node, "status": status, "result": result,
         "at": datetime.now(UTC).isoformat()}), flush=True, file=sys.stdout)


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


@dataclass(frozen=True)
class ScannedDeskReport:
    """T7 = scan -> desk, one attention+analysis stage: the scan line is
    prepended to the DeskReport summary so the node line reads
    'scanned 112 -> desk 5 (+2 held) · memos ...'."""
    scan_line: str
    desk: Any  # DeskReport-shaped: anything with .summary()
    scan_failed: bool = False

    def summary(self) -> str:
        return f"{self.scan_line} · {self.desk.summary()}"


def build_scanned_desk(run_desk: Callable[[Session, Clock, list[str]], Any],
                       desk_symbols: Callable[[Session], list[str]],
                       *, top_n: int = 5) -> Callable[[Session, Clock], ScannedDeskReport]:
    """The T7 desk callable: deterministic scan first (ADR-0007), then the LLM
    desk on exactly the shortlist. Fail-soft: if scanning raises, the desk
    runs on the full eligible universe (desk_symbols) instead — the desk must
    not go blind because ranking broke — and scan_failed=True makes the run
    page like a desk failure. This covers ranking bugs, not a dead database:
    a scan failure that aborts the transaction kills the run like any other
    node-level SQL failure."""
    def scanned_desk(session: Session, clock: Clock) -> ScannedDeskReport:
        try:
            report = scan(session, clock, top_n=top_n)
        except Exception as e:  # noqa: BLE001 — fail-soft, but never silent
            fallback = run_desk(session, clock, desk_symbols(session))
            return ScannedDeskReport(
                scan_line=f"scan FAILED ({str(e)[:120]}) -> desk full eligible universe",
                desk=fallback, scan_failed=True)
        desk_report = run_desk(session, clock, [e.symbol for e in report.shortlist])
        return ScannedDeskReport(
            scan_line=(f"scanned {report.scanned} -> desk {report.n_scored} "
                       f"(+{report.n_held} held)"),
            desk=desk_report)
    return scanned_desk


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
        if getattr(report, "scan_failed", False):
            # the scanner broke and the desk fell back to the full eligible
            # universe (ADR-0007 fail-soft): the memos still landed, but the
            # run pages exactly like a desk failure so a broken ranker is
            # never silently absorbed
            state["desk_failed"] = True
        return report.summary()

    def t8_bridge() -> str:
        # runs desk or no desk: memos from a manually-run desk (or an earlier
        # cycle) must still bridge — candidacy lives in the memos table, not
        # in this run's T7 result (ADR-0006)
        try:
            report = bridge_memos(session, clock)
        except Exception as e:  # noqa: BLE001 — a bridge failure pages, but
            #                     the settled, protected book stands untouched
            state["bridge_failed"] = True
            return f"bridge FAILED: {e}"[:300]
        state["bridge"] = report
        return report.summary()

    def t9_report() -> str:
        # scorecard FIRST (memo outcomes mature on this cycle's freshly
        # ingested bars): fail-soft exactly like the desk/bridge — the failure
        # is noted in its line and pages, but the report always lands. A SQL
        # failure that aborts the transaction still kills the run like any
        # other node-level SQL failure (same caveat as the scanner's).
        try:
            scorecard_line = compute_memo_outcomes(session, clock).summary()
        except Exception as e:  # noqa: BLE001 — fail-soft, but never silent
            state["scorecard_failed"] = True
            scorecard_line = f"scorecard FAILED: {e}"[:200]
        ingest = state.get("ingest")
        stops = state.get("stops", ())
        fills = state.get("fills", ())
        nav = state.get("nav", Decimal(0))
        desk_report = state.get("desk")
        bridge_report = state.get("bridge")
        failed = (bool(state.get("ingest_failed", False))
                  or bool(state.get("desk_failed", False))
                  or bool(state.get("bridge_failed", False))
                  or bool(state.get("scorecard_failed", False)))
        lines = [f"NAV A${nav}",
                 f"fills {len(fills)}, stops fired {len(stops)}",  # type: ignore[arg-type]
                 desk_report.summary() if desk_report is not None else "desk idle",
                 bridge_report.summary() if bridge_report is not None else "bridge idle",
                 scorecard_line,
                 "ingest FAILED — see log" if bool(state.get("ingest_failed", False))
                 else "ingest clean"]
        if state.get("desk_failed"):
            lines.append("desk FAILED — see log")
        if state.get("bridge_failed"):
            lines.append("bridge FAILED — see log")
        summary = " · ".join(lines)
        PostgresAuditLog(session, clock).append(
            event_type="daily_cycle.completed", entity_type="pipeline",
            entity_id=day, actor_type="dcp", actor_id="daily_pipeline",
            payload={"summary": summary, "ingest_failed": failed,
                     "ingest_failures": list(getattr(ingest, "failures", []))})
        notify(f"Atlas daily {day}", summary,
               priority="high" if failed else "default")
        return summary

    def _live(name, fn):
        """Wrap a node with @@CYCLE progress lines on stdout: the run is ONE
        uncommitted transaction, so the DB shows nothing mid-run — this stream
        is how the console animates the cycle in real time (the scheduler
        captures it; a plain CLI run just prints it)."""
        def wrapped():
            _emit(name, "running")
            try:
                result = fn()
            except Exception as e:
                _emit(name, "failed", str(e)[:200])
                raise
            _emit(name, "done", result)
            return result
        return wrapped

    runner = WorkflowRunner(session, PostgresAuditLog(session, clock), clock)
    nodes = [
        Node("t0_ingest", t0_ingest),
        Node("t1_verify_chain", t1_verify_chain),
        Node("t2_expire", t2_expire),
        Node("t3_settle", t3_settle),
        Node("t4_stops", t4_stops),
        Node("t5_snapshot", t5_snapshot),
        Node("t6_reconcile", t6_reconcile),
        Node("t7_desk", t7_desk),
        Node("t8_bridge", t8_bridge),
        Node("t9_report", t9_report),
    ]
    return runner.run(f"daily-{day}", [Node(n.name, _live(n.name, n.fn))
                                       for n in nodes])


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

        # scan first (ADR-0007): the desk studies the scanner's shortlist,
        # never the full universe — breadth is the scanner's job, and it's free
        desk = build_scanned_desk(run_desk, desk_symbols)
    with session_scope() as s:
        results = run_daily_cycle(s, clock, adapter, desk=desk)
    ingest_line = results.get("t0_ingest") or ""
    failed = ("failed=True" in ingest_line
              or "desk FAILED" in (results.get("t7_desk") or "")
              or "scan FAILED" in (results.get("t7_desk") or "")
              or "bridge FAILED" in (results.get("t8_bridge") or ""))
    print(json.dumps(results, indent=2))
    raise SystemExit(2 if failed else 0)


if __name__ == "__main__":
    main()
