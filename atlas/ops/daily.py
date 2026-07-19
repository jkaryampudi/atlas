"""T0–T9 daily operating cycle (Doc 01 §5, Doc 08 Phase 5) — the loop that
makes Atlas LIVE: one checkpointed WorkflowRunner graph per calendar day,
fired by launchd at 09:30 AEST (after the US close and EODHD publish).

  T0 ingest       incremental bars + FX + splits, quality gates written
  T1 verify       audit hash chain end-to-end (tamper = the run fails)
  T2 expire       proposals past their 24h TTL
  T3 settle       pending orders fill at their session opens
  T4 stops        protective stop scan — pre-authorized exits fire
  T5 snapshot     mark the book -> trading.portfolio_snapshots (+ breaker fold)
  T5b bands       ADR-0010 tolerance-band check: record quant.sleeve_daily and
                  demote a breaching paper strategy to 'suspended' (latching;
                  atlas/dcp/trading/bands.py). Fail-soft: a band-check failure
                  pages but never undoes the settled, protected book
  T5c cusum       drift EARLY-WARNING (board item 7): replay the CUSUM
                  detector over the stored sleeve series against the derived
                  contract's parameters (tolerance_bands.cusum). A latched
                  breach audits + PAGES for Principal review but NEVER
                  demotes — demotion authority stays with the t5b bands
                  (atlas/dcp/trading/bands.py check_cusum documents the
                  signed rationale). Fail-soft exactly like t5b
  T6 reconcile    internal consistency: positions ≡ open lots, ledger cash
                  finite and NAV recomputable; writes trading.reconciliations
  T6b signals     xsmom paper-strategy signal generation (ADR-0010, migration
                  0020): monthly rebalance at the month-end session, one-time
                  initiation after approval, quant.signals upsert
                  (atlas/dcp/signals/xsmom/generate.py). Fail-soft like the
                  desk — a signal failure must never kill settlement
  T6c pead signals PEAD/SUE paper-strategy signal generation (ADR-0013/0014):
                  the second satellite sleeve, monthly rebalance / one-time
                  initiation / catch_up exactly like t6b, quant.signals upsert
                  (atlas/dcp/signals/pead/generate.py). Fail-soft like t6b
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
  T8b attribution daily core(beta)/satellite(alpha) NAV decomposition (ADR-0012
                  consequence 4, Doc 04 §14 substrate): upsert today's
                  reporting.attribution_daily rows from the T5 snapshot
                  (atlas/dcp/reporting/attribution.py — flow-adjusted returns,
                  SPY-TR / 55:15-blend benchmarks). Fail-soft exactly like t5b:
                  a failed decomposition pages, the settled and protected book
                  stands, and t9 still reports
  T8c core        standing-core maintenance (ADR-0012 + ops-reliability build):
                  the passive core is a STANDING POLICY, not a timely signal —
                  if the book is outside its drift band and no live core
                  proposal covers a leg, regenerate it through the existing
                  build_core_proposals path (risk-checked, 72h TTL) so the
                  core is ALWAYS one click away each morning; live proposals
                  are never duplicated, expired/rejected ones stay history
                  (atlas/dcp/trading/core_allocation.maintain_core_proposals).
                  Fail-soft exactly like t5b
  T9 report       scorecard + learning + source-pick grading + the monthly
                  opportunity-screen cohort (self-healing: first cycle of a
                  month with no cohort records the board's top-K as measured
                  picks — atlas/ops/screen.monthly_snapshot_if_due), then the
                  summary incl. desk + bridge + attribution + core results ->
                  audit + operator alert
  T9b brief       the Principal's morning brief: ASSEMBLE (never compute) the
                  session's cycle results, approval queue with expiry
                  countdowns, memos, attribution, band/CUSUM status, learning
                  line, budget spend and urgent alerts into ONE persisted
                  jsonb row (reporting.morning_brief, migration 0031; GET
                  /v1/reporting/brief/latest; the console BRIEF card). Also
                  fires the once-per-proposal expiring-soon pages
                  (atlas/ops/alerts.check_expiring_proposals). Runs AFTER t9
                  so the brief sees every node line including t9's own.
                  Fail-soft: a brief failure flags the run's exit code (the
                  scheduler pages) but never touches the book or the report

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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock, FrozenClock, SystemClock
from atlas.core.db import session_scope
from atlas.core.workflow import Node, WorkflowRunner
from atlas.dcp.market_data.adapters.base import MarketDataAdapter
from atlas.dcp.market_data.calendars import is_trading_day, session_close_utc
from atlas.dcp.market_data.daily import DailyIngestReport, run_daily_ingest
from atlas.dcp.learning.loop import run_learning
from atlas.dcp.reporting.attribution import compute_attribution_day
from atlas.dcp.reporting.brief import persist_brief
from atlas.dcp.scanner.v1 import scan
from atlas.dcp.scorecard import compute_memo_outcomes, vendor_adapter_for
from atlas.dcp.signals.pead.generate import (
    active_pead_signal_symbols,
    generate_pead_signals,
)
from atlas.dcp.signals.xsmom.generate import active_signal_symbols, generate_signals
from atlas.dcp.trading.bands import check_bands, check_cusum
from atlas.dcp.trading.bridge import bridge_memos
from atlas.dcp.trading.core_allocation import maintain_core_proposals
from atlas.dcp.trading.exits import scan_stop_exits
from atlas.dcp.trading.proposals import expire_stale, settle_orders, snapshot
from atlas.ops.alerts import (
    check_expiring_proposals,
    maybe_billing_outage_alert,
    notify,
)
from atlas.tools.verify_chain import run as verify_chain


def _emit(node: str, status: str, result: str | None = None) -> None:
    """One machine-readable progress line per node transition."""
    print("@@CYCLE " + json.dumps(
        {"node": node, "status": status, "result": result,
         "at": datetime.now(UTC).isoformat()}), flush=True, file=sys.stdout)


# Session-close guard (production defect 2026-07-13): a console "Run Cycle
# now" click at 11:07 AEST created and COMPLETED the daily-2026-07-13
# checkpoint before Monday's US session had traded a single bar (t0 ingested
# 0 new bars). The run_id is one-per-date and resume-safe, so the evening
# scheduler firing merely replayed the finished checkpoint: the desk debated
# Friday's closes and the day's bridge slot was spent on stale data. The fix
# is structural — the cycle REFUSES to start for a US (XNYS) session date
# until that session has closed, per the exchange calendar and the injected
# clock, BEFORE the checkpoint row exists, so a refused attempt never
# consumes the day.
#
# WHY 30 minutes of grace: the closing bell is not the data — EODHD's
# end-of-day file is never complete at 20:00:00 UTC sharp, and this guard is
# deliberately structural (calendar + clock only, no vendor calls), so it
# cannot ask whether the bars have actually landed. 30 minutes blunts the
# "click right at the close" edge of the same defect (a 20:01 UTC click would
# still ingest nothing) while staying far clear of the scheduled 23:30 UTC
# firing: the latest XNYS close all year is 21:00 UTC (winter), and 21:30 <
# 23:30 on every session — pinned across a full calendar year by
# tests/unit/test_cycle_guard.py. It is a hedge, not a publication guarantee;
# the t0 quality gates remain the authority on whether the bars arrived.
CYCLE_EARLIEST_AFTER_CLOSE_MIN = 30

# Distinct from 0 (clean day) and 2 (a node/ingest failure): "come back after
# the close" is neither — the scheduler must not page FAILED for it, and cron/
# launchd logs must still be able to tell the three apart.
EXIT_REFUSED = 3


class CycleRefusedError(RuntimeError):
    """The cycle declined to START for this date. Raised before the
    daily-<date> checkpoint row is created, so the day is NOT consumed:
    a later, post-close invocation is a fresh full run."""


def cycle_refusal(clock: Clock) -> str | None:
    """Reason the daily cycle must not start yet, or None to proceed.

    Structural only — exchange calendar + injected clock, never a vendor
    call. For a US (XNYS) session date D, refuse until D's session close
    plus CYCLE_EARLIEST_AFTER_CLOSE_MIN. Non-session days (weekends,
    holidays) pass through unchanged: that path already runs and honestly
    records "not a US session". The scheduler's own 23:30 UTC firing passes
    trivially on every session of the year (early closes included).
    """
    now = clock.now().astimezone(UTC)
    day = now.date()  # same derivation as the daily-<date> run_id
    if not is_trading_day("US", day):
        return None
    close = session_close_utc("US", day)
    earliest = close + timedelta(minutes=CYCLE_EARLIEST_AFTER_CLOSE_MIN)
    if now >= earliest:
        return None
    return (f"cycle for {day} refused: US session not yet closed "
            f"(closes {close:%H:%M} UTC + {CYCLE_EARLIEST_AFTER_CLOSE_MIN}min "
            f"vendor grace); re-run after {earliest:%H:%M} UTC")


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


def merge_shortlist(priority: list[str], rest: list[str]) -> list[str]:
    """Signal-first desk ordering (ADR-0010 wiring): the approved strategy's
    active signal names lead, then the scanner's picks, deduped preserving
    first occurrence. ORDER IS POLICY: the nightly budget breaker halts the
    TAIL of the shortlist, so the paper sleeve's names must never be the ones
    starved when the cap bites."""
    return list(dict.fromkeys([*priority, *rest]))


def build_scanned_desk(run_desk: Callable[[Session, Clock, list[str]], Any],
                       desk_symbols: Callable[[Session], list[str]],
                       *, top_n: int = 5) -> Callable[[Session, Clock], ScannedDeskReport]:
    """The T7 desk callable: ACTIVE SIGNAL NAMES first (ADR-0010 — signals
    with valid_until >= session and no non-expired proposal standing), then
    the deterministic scan (ADR-0007), the LLM desk on exactly the merged
    shortlist. Fail-soft: if scanning raises, the desk runs on signals + the
    full eligible universe (desk_symbols) instead — the desk must not go
    blind because ranking broke — and scan_failed=True makes the run page
    like a desk failure. This covers ranking bugs, not a dead database: a
    scan failure that aborts the transaction kills the run like any other
    node-level SQL failure."""
    def scanned_desk(session: Session, clock: Clock) -> ScannedDeskReport:
        # BOTH satellite sleeves lead the shortlist (ADR-0010/0013/0014):
        # momentum's active names, then PEAD's, deduped preserving order — so
        # neither signed sleeve is starved when the nightly budget breaker
        # halts the tail (order is policy, module docstring).
        signal_names = merge_shortlist(active_signal_symbols(session, clock),
                                       active_pead_signal_symbols(session, clock))
        try:
            report = scan(session, clock, top_n=top_n)
        except Exception as e:  # noqa: BLE001 — fail-soft, but never silent
            fallback = run_desk(session, clock,
                                merge_shortlist(signal_names, desk_symbols(session)))
            return ScannedDeskReport(
                scan_line=f"scan FAILED ({str(e)[:120]}) -> desk full eligible universe",
                desk=fallback, scan_failed=True)
        shortlist = merge_shortlist(signal_names,
                                    [e.symbol for e in report.shortlist])
        desk_report = run_desk(session, clock, shortlist)
        return ScannedDeskReport(
            scan_line=(f"signals {len(signal_names)} + scanned {report.scanned} "
                       f"-> desk {len(shortlist)} ({report.n_held} held)"),
            desk=desk_report)
    return scanned_desk


def run_daily_cycle(session: Session, clock: Clock, adapter: MarketDataAdapter,
                    desk: Callable[[Session, Clock], Any] | None = None,
                    ) -> dict[str, str | None]:
    """One day's full cycle under a single checkpointed run_id. Returns the
    node-result map; raises on kill conditions (chain break, recon break) and
    CycleRefusedError when the run date's US session has not closed yet."""
    # the guard sits ABOVE the WorkflowRunner on purpose: a refusal must leave
    # no workflow.workflow_runs row behind, or the refused attempt would
    # consume the one-per-date checkpoint exactly like the 2026-07-13 defect
    refusal = cycle_refusal(clock)
    if refusal is not None:
        _emit("guard", "refused", refusal)
        raise CycleRefusedError(refusal)
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

    def t5b_bands() -> str:
        # ADR-0010 accountability loop: the sleeve series lands every cycle
        # and a breach demotes the strategy row (latching). Fail-soft exactly
        # like the desk — the book is already settled and protected; a SQL
        # failure that aborts the transaction still kills the run (same
        # caveat as the scanner's).
        try:
            report = check_bands(session, clock)
        except Exception as e:  # noqa: BLE001 — fail-soft, but never silent
            state["bands_failed"] = True
            return f"bands FAILED: {e}"[:300]
        state["bands"] = report
        return report.summary()

    def t5c_cusum() -> str:
        # Drift early-warning (board item 7): pages, never demotes — see
        # bands.check_cusum for the signed rationale. Fail-soft exactly like
        # t5b: the book is already settled and protected; a SQL failure that
        # aborts the transaction still kills the run (same caveat).
        try:
            report = check_cusum(session, clock)
        except Exception as e:  # noqa: BLE001 — fail-soft, but never silent
            state["cusum_failed"] = True
            return f"cusum FAILED: {e}"[:300]
        state["cusum"] = report
        return report.summary()

    def t6_reconcile() -> str:
        return _reconcile(session, clock)

    def t6b_signals() -> str:
        # xsmom paper signals (ADR-0010): runs BEFORE the desk so tonight's
        # rebalance names reach tonight's shortlist with citable evidence.
        # Fail-soft: a generation failure pages, the trading steps stand.
        try:
            report = generate_signals(session, clock)
        except Exception as e:  # noqa: BLE001 — fail-soft, but never silent
            state["signals_failed"] = True
            return f"signals FAILED: {e}"[:300]
        state["signals"] = report
        return report.summary()

    def t6c_pead_signals() -> str:
        # PEAD paper signals (ADR-0013/0014): the second satellite sleeve,
        # generated in parallel to xsmom (t6b) and BEFORE the desk so tonight's
        # PEAD rebalance names reach tonight's shortlist with citable evidence.
        # Fail-soft exactly like t6b: a generation failure pages, the settled
        # and protected book stands.
        try:
            report = generate_pead_signals(session, clock)
        except Exception as e:  # noqa: BLE001 — fail-soft, but never silent
            state["pead_signals_failed"] = True
            return f"pead signals FAILED: {e}"[:300]
        state["pead_signals"] = report
        return report.summary()

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
            # Billing-outage detector (ops-reliability build): a NON-TRANSIENT
            # client HTTP error (4xx≠429 — runner.py propagates that class
            # raw; the vendor's 400-credit signature) with ZERO completed LLM
            # calls today pages ONCE per day at high priority and lands an
            # audit event the morning brief reads. Four silent billing
            # outages in five days are why this exists.
            if maybe_billing_outage_alert(session, clock, exc=e):
                state["billing_outage"] = True
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

    def t8b_attribution() -> str:
        # ADR-0012 consequence 4: decompose tonight's snapshot into core
        # (beta) / satellite (alpha) / cash and upsert the daily rows.
        # Fail-soft exactly like t5b — the book is already settled and
        # protected; a SQL failure that aborts the transaction still kills
        # the run (same caveat as the scanner's).
        try:
            report = compute_attribution_day(session, clock)
        except Exception as e:  # noqa: BLE001 — fail-soft, but never silent
            state["attribution_failed"] = True
            return f"attribution FAILED: {e}"[:300]
        if report is None:
            return "attribution idle (no snapshot yet)"
        state["attribution"] = report
        return report.summary()

    def t8c_core() -> str:
        # Standing-core maintenance (module docstring): the drift band is a
        # STANDING policy, so this runs every cycle — regenerate any uncovered
        # drifted leg (72h TTL, risk-checked), never duplicate a live one.
        # Fail-soft exactly like t5b: the settled, protected book stands; a
        # SQL failure that aborts the transaction still kills the run (same
        # caveat as the scanner's).
        try:
            report = maintain_core_proposals(session, clock)
        except Exception as e:  # noqa: BLE001 — fail-soft, but never silent
            state["core_failed"] = True
            return f"core FAILED: {e}"[:300]
        state["core"] = report
        return report.summary()

    def t9_report() -> str:
        # scorecard FIRST (memo outcomes mature on this cycle's freshly
        # ingested bars): fail-soft exactly like the desk/bridge — the failure
        # is noted in its line and pages, but the report always lands. A SQL
        # failure that aborts the transaction still kills the run like any
        # other node-level SQL failure (same caveat as the scanner's).
        try:
            # adapter_for enables the bounded analysis-only bar top-up (desk-
            # review 2026-07 item 5): inactive instruments are skipped by t0
            # ingest, so the scorecard fetches their missing forward window
            # itself — pure addition; None would keep t9 a pure read
            scorecard_line = compute_memo_outcomes(
                session, clock, adapter_for=vendor_adapter_for).summary()
        except Exception as e:  # noqa: BLE001 — fail-soft, but never silent
            state["scorecard_failed"] = True
            scorecard_line = f"scorecard FAILED: {e}"[:200]
        # learning loop AFTER the scorecard (labels grade tonight's freshly
        # matured outcomes): fail-soft exactly like the scorecard — surfacing
        # only (Article 10 v1: label + measure, never apply), so a failure
        # here can never touch the settled, protected book. Same SQL caveat.
        try:
            learning_line = run_learning(session, clock).summary()
        except Exception as e:  # noqa: BLE001 — fail-soft, but never silent
            state["learning_failed"] = True
            learning_line = f"learning FAILED: {e}"[:200]
        # source-pick grading (external lists, e.g. investing.com): mature the
        # 20/60-session excess-vs-SPY outcome on tonight's freshly ingested
        # bars — the same as the scorecard, so a monthly list grades itself
        # with no console click. Fail-soft, surfacing-only (measured, never
        # applied): a failure here can never touch the settled book.
        try:
            from atlas.dcp.research.source_picks import grade_picks
            pg = grade_picks(session, clock)
            picks_line = (f"source-picks graded {pg.graded}, "
                          f"{pg.still_immature} immature")
        except Exception as e:  # noqa: BLE001 — fail-soft, but never silent
            state["picks_failed"] = True
            picks_line = f"source-picks FAILED: {e}"[:200]
        # monthly opportunity-screen cohort (self-healing: the first cycle of a
        # month with no cohort records the board's top-K as measured picks, so
        # the screen's edge trial sustains itself with no console click).
        # Fail-soft, surfacing-only — deterministic, no model spend, and like
        # every source-pick it is measured, never bridged. Same SQL caveat.
        try:
            from atlas.ops.screen import monthly_snapshot_if_due
            screen_line = monthly_snapshot_if_due(session, clock)
        except Exception as e:  # noqa: BLE001 — fail-soft, but never silent
            state["screen_snapshot_failed"] = True
            screen_line = f"screen-cohort FAILED: {e}"[:200]
        ingest = state.get("ingest")
        stops = state.get("stops", ())
        fills = state.get("fills", ())
        nav = state.get("nav", Decimal(0))
        desk_report = state.get("desk")
        bridge_report = state.get("bridge")
        signals_report = state.get("signals")
        pead_signals_report = state.get("pead_signals")
        bands_report = state.get("bands")
        cusum_report = state.get("cusum")
        attribution_report = state.get("attribution")
        core_report = state.get("core")
        failed = (bool(state.get("ingest_failed", False))
                  or bool(state.get("desk_failed", False))
                  or bool(state.get("bridge_failed", False))
                  or bool(state.get("scorecard_failed", False))
                  or bool(state.get("learning_failed", False))
                  or bool(state.get("signals_failed", False))
                  or bool(state.get("pead_signals_failed", False))
                  or bool(state.get("bands_failed", False))
                  or bool(state.get("cusum_failed", False))
                  or bool(state.get("attribution_failed", False))
                  or bool(state.get("core_failed", False))
                  or bool(state.get("picks_failed", False))
                  or bool(state.get("screen_snapshot_failed", False)))
        lines = [f"NAV A${nav}",
                 f"fills {len(fills)}, stops fired {len(stops)}",  # type: ignore[arg-type]
                 desk_report.summary() if desk_report is not None else "desk idle",
                 bridge_report.summary() if bridge_report is not None else "bridge idle",
                 signals_report.summary() if signals_report is not None else "signals idle",
                 pead_signals_report.summary() if pead_signals_report is not None
                 else "pead signals idle",
                 bands_report.summary() if bands_report is not None else "bands idle",
                 cusum_report.summary() if cusum_report is not None else "cusum idle",
                 attribution_report.summary() if attribution_report is not None
                 else "attribution idle",
                 core_report.summary() if core_report is not None
                 else "core idle",
                 scorecard_line,
                 learning_line,
                 picks_line,
                 screen_line,
                 "ingest FAILED — see log" if bool(state.get("ingest_failed", False))
                 else "ingest clean"]
        if state.get("desk_failed"):
            lines.append("desk FAILED — see log")
        if state.get("bridge_failed"):
            lines.append("bridge FAILED — see log")
        if state.get("learning_failed"):
            lines.append("learning FAILED — see log")
        if state.get("signals_failed"):
            lines.append("signals FAILED — see log")
        if state.get("pead_signals_failed"):
            lines.append("pead signals FAILED — see log")
        if state.get("bands_failed"):
            lines.append("bands FAILED — see log")
        if state.get("cusum_failed"):
            lines.append("cusum FAILED — see log")
        if state.get("attribution_failed"):
            lines.append("attribution FAILED — see log")
        if state.get("core_failed"):
            lines.append("core FAILED — see log")
        if state.get("picks_failed"):
            lines.append("source-picks FAILED — see log")
        if state.get("screen_snapshot_failed"):
            lines.append("screen-cohort FAILED — see log")
        if state.get("billing_outage"):
            lines.append("BILLING OUTAGE — API credits exhausted, desk skipped")
        summary = " · ".join(lines)
        PostgresAuditLog(session, clock).append(
            event_type="daily_cycle.completed", entity_type="pipeline",
            entity_id=day, actor_type="dcp", actor_id="daily_pipeline",
            payload={"summary": summary, "ingest_failed": failed,
                     "ingest_failures": list(getattr(ingest, "failures", []))})
        notify(f"Atlas daily {day}", summary,
               priority="high" if failed else "default")
        return summary

    def t9b_brief() -> str:
        # Morning brief + expiring-proposal pages (module docstring). Alerts
        # run FIRST so tonight's ops.alert.urgent events are already on the
        # chain when the brief reads them. Fail-soft: t9's report and page
        # already landed; a brief failure flags the exit code (the scheduler
        # pages FAILED) but never undoes anything. Same SQL-abort caveat as
        # the scanner's.
        try:
            fired = check_expiring_proposals(session, clock)
            brief = persist_brief(session, clock)
        except Exception as e:  # noqa: BLE001 — fail-soft, but never silent
            state["brief_failed"] = True
            return f"brief FAILED: {e}"[:300]
        line = brief.summary()
        if fired:
            line += f" · paged {len(fired)} expiring proposal(s)"
        return line

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
        Node("t5b_bands", t5b_bands),
        Node("t5c_cusum", t5c_cusum),
        Node("t6_reconcile", t6_reconcile),
        Node("t6b_signals", t6b_signals),
        Node("t6c_pead_signals", t6c_pead_signals),
        Node("t7_desk", t7_desk),
        Node("t8_bridge", t8_bridge),
        Node("t8b_attribution", t8b_attribution),
        Node("t8c_core", t8c_core),
        Node("t9_report", t9_report),
        Node("t9b_brief", t9b_brief),
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
    try:
        with session_scope() as s:
            results = run_daily_cycle(s, clock, adapter, desk=desk)
    except CycleRefusedError as e:
        # a polite, deliberate no-op: nothing was written (no checkpoint row,
        # no audit event), the day is NOT consumed, and the distinct exit code
        # keeps the scheduler from paging FAILED for a run that simply came
        # before the close — the plain line below is what lands in the
        # scheduler's status detail
        print(f"REFUSED: {e}")
        raise SystemExit(EXIT_REFUSED) from None
    ingest_line = results.get("t0_ingest") or ""
    failed = ("failed=True" in ingest_line
              or "desk FAILED" in (results.get("t7_desk") or "")
              or "scan FAILED" in (results.get("t7_desk") or "")
              or "bridge FAILED" in (results.get("t8_bridge") or "")
              or "signals FAILED" in (results.get("t6b_signals") or "")
              or "pead signals FAILED" in (results.get("t6c_pead_signals") or "")
              or "bands FAILED" in (results.get("t5b_bands") or "")
              or "cusum FAILED" in (results.get("t5c_cusum") or "")
              or "attribution FAILED" in (results.get("t8b_attribution") or "")
              or "core FAILED" in (results.get("t8c_core") or "")
              or "brief FAILED" in (results.get("t9b_brief") or "")
              # t9's internal fail-soft lines (scorecard / learning / source-
              # picks / screen-cohort): they already page via the high-priority
              # notify, but the exit code must agree so a webhook-less install
              # still sees the failure in the scheduler status (review 2026-07)
              or "FAILED" in (results.get("t9_report") or ""))
    print(json.dumps(results, indent=2))
    raise SystemExit(2 if failed else 0)


if __name__ == "__main__":
    main()
