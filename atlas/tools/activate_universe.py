"""S&P 500 universe activation + membership reconciliation (ADR-0016).

THE mechanism decisions 1 and 4 call for: ``sync_universe`` deliberately never
touches ``is_active`` (that neutrality is preserved — this is a SEPARATE,
reviewed, one-shot tool), so flipping the ~400 inactive current members active
— and, semi-annually, reconciling drift — happens here and only here.

ELIGIBILITY (fail-closed; every gate must pass, all failures recorded):
a member activates iff it is a current constituent (``is_active_now`` in
validation.index_membership), its ticker matches exactly ONE market='US'
instrument row, it is NOT vendor-delisted (the AGN trap: one current member is
a dead ticker, last bar 2020-05-08, carrying is_active_now=true AND
is_delisted=true — reconciling against is_delisted alone is not enough, hence
the bar-recency gate below), it has a real ``sector_gics`` (GICS backfill runs
FIRST — decision 2; run atlas.tools.backfill_gics before this tool, or
everything fails the no-sector gate and the sanity band refuses), and its
latest stored bar is within MAX_STALE_SESSIONS XNYS sessions of FRESH_REF.

FRESHNESS RULE: FRESH_REF is the PIT backfill end (index_membership.PRICE_END,
2026-07-10 — single source of truth); MAX_STALE_SESSIONS = 10 (~2 calendar
weeks). The ~400 targets sit exactly AT the backfill end (0 sessions stale),
so 10 tolerates a short gap between backfill and activation while excluding
anything dead for months (AGN: ~1,550 sessions stale). A future reconcile run
after the nightly has been advancing bars should pass ``--fresh-ref`` as the
last completed session (or bump PRICE_END deliberately, in review).

SANITY BAND (fat-finger guard): --apply REFUSES unless the would-activate
count lies in [SANITY_MIN, SANITY_MAX] = [350, 420]. Expectation on dev is
~400 (401 inactive current members minus AGN minus any unresolved sectors);
the band admits legitimate small drift but refuses a broken join (0, 101,
502) or a fat-fingered membership snapshot. There is NO bypass flag: if a
count outside the band is ever genuinely right, change the constant in a
reviewed commit. Reconcile mode has its own cap: RECONCILE_MAX_CHANGES = 40
total flips (S&P churn is ~20-25 names/YEAR, so a semi-annual reconcile
should see well under 40; more means a corrupted snapshot).

DEACTIVATION SEMANTICS (decision 4): reconcile mode flips members no longer
``is_active_now`` to is_active=false — stocks only, never ETFs/ADRs that
merely share a former member's ticker. Deactivation ONLY stops new signals
and nightly ingest (both select on is_active); open positions in a
deactivated name are untouched and exit via the normal paths (pre-authorized
stop exits / discretionary close, atlas/dcp/trading/exits.py). Ticker-reuse
caveat: the vendor membership table is ticker-keyed and demonstrably confuses
reused tickers (index_membership module docstring), so reconcile output is
human-reviewed at the dry-run stage before --apply — the tool is the
mechanism, the Principal is the authority.

MANIFEST: --apply extends seeds/universe.json with the activated names (and
reconcile removes deactivated stocks) so the reviewed manifest matches
reality, regenerated deterministically sorted by (symbol, exchange).
sync_universe stays is_active-neutral, so syncing the extended manifest
remains safe in both directions.

CATCH-UP (decision 3): no manual backfill after activation — the first
post-activation nightly advances every newly-active name from its own latest
stored bar (~5 sessions) and fetches fundamentals/earnings/estimates; the
quality gates stay untouched and will honestly RED any name the vendor
cannot serve.

Every apply appends exactly ONE audit event carrying the full activated /
deactivated symbol lists and every exclusion with its reasons. Dry runs
(the default) write NOTHING — no UPDATE, no manifest write, no audit event.

Usage:
  python -m atlas.tools.activate_universe                    # dry-run plan
  python -m atlas.tools.activate_universe --apply            # one-shot flip
  python -m atlas.tools.activate_universe --reconcile        # drift dry-run
  python -m atlas.tools.activate_universe --reconcile --apply
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.dcp.market_data.calendars import previous_trading_day
from atlas.dcp.market_data.index_membership import PRICE_END
from atlas.dcp.market_data.universe import REQUIRED_FIELDS as MANIFEST_FIELDS

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SEEDS = ROOT / "seeds" / "universe.json"

INDEX_CODE = "GSPC.INDX"
FRESH_REF = PRICE_END           # PIT backfill end, 2026-07-10 (single source)
MAX_STALE_SESSIONS = 10         # XNYS sessions; rationale in module docstring
SANITY_MIN = 350                # would-activate band; rationale above;
SANITY_MAX = 420                # no bypass flag — change only in review
RECONCILE_MAX_CHANGES = 40      # semi-annual drift cap; rationale above


def freshness_threshold(ref: date = FRESH_REF,
                        sessions: int = MAX_STALE_SESSIONS) -> date:
    """The earliest acceptable latest-bar date: `sessions` XNYS sessions
    before `ref` (holidays skipped by the exchange calendar, never guessed)."""
    day = ref
    for _ in range(sessions):
        day = previous_trading_day("US", day)
    return day


@dataclass(frozen=True)
class UniversePlan:
    mode: str                                   # "activate" | "reconcile"
    to_activate: tuple[str, ...]
    to_deactivate: tuple[str, ...]              # always () in activate mode
    already_active: tuple[str, ...]
    excluded: tuple[tuple[str, tuple[str, ...]], ...]   # (symbol, reasons)
    current_members: int
    fresh_ref: date
    max_stale_sessions: int
    threshold: date


@dataclass(frozen=True)
class AppliedUniverseChange:
    event_type: str
    activated: tuple[str, ...]
    deactivated: tuple[str, ...]
    seeds_added: tuple[str, ...]
    seeds_removed: tuple[str, ...]


def build_plan(session: Session, *, mode: str = "activate",
               index_code: str = INDEX_CODE, fresh_ref: date = FRESH_REF,
               max_stale_sessions: int = MAX_STALE_SESSIONS) -> UniversePlan:
    """Read-only selection: SELECTs only, writes nothing (the dry-run IS this
    plan, printed). Exclusion reasons are cumulative per symbol — a corpse
    that is both vendor-delisted and stale reports both facts."""
    if mode not in ("activate", "reconcile"):
        raise ValueError(f"unknown mode {mode!r}")
    threshold = freshness_threshold(fresh_ref, max_stale_sessions)

    rows = session.execute(text(
        "SELECT m.ticker, m.is_delisted, i.id AS iid, i.is_active, "
        "       i.sector_gics, "
        "       (SELECT max(pb.bar_date) FROM market.price_bars_daily pb "
        "         WHERE pb.instrument_id = i.id) AS last_bar "
        "FROM validation.index_membership m "
        "LEFT JOIN market.instruments i "
        "       ON i.symbol = m.ticker AND i.market = 'US' "
        "WHERE m.index_code = :ic AND m.is_active_now "
        "ORDER BY m.ticker"), {"ic": index_code}).mappings().all()
    by_ticker: dict[str, list[Mapping[str, object]]] = {}
    for r in rows:
        by_ticker.setdefault(str(r["ticker"]), []).append(r)

    to_activate: list[str] = []
    already_active: list[str] = []
    excluded: list[tuple[str, tuple[str, ...]]] = []
    for ticker in sorted(by_ticker):
        matches = by_ticker[ticker]
        if len(matches) == 1 and matches[0]["iid"] is None:
            excluded.append((ticker, ("no-instrument",)))
            continue
        if len(matches) > 1:
            excluded.append((ticker, ("ambiguous-instrument",)))
            continue
        row = matches[0]
        if row["is_active"]:
            already_active.append(ticker)       # no-op whatever else is true
            continue
        reasons: list[str] = []
        if row["is_delisted"]:
            reasons.append("vendor-delisted")
        if row["sector_gics"] in (None, ""):
            reasons.append("no-sector")
        last_bar = row["last_bar"]
        if last_bar is None:
            reasons.append("no-bars")
        elif not isinstance(last_bar, date) or last_bar < threshold:
            reasons.append("stale-bars")
        if reasons:
            excluded.append((ticker, tuple(reasons)))
        else:
            to_activate.append(ticker)

    to_deactivate: list[str] = []
    if mode == "reconcile":
        drop_rows = session.execute(text(
            "SELECT i.symbol, count(*) AS n "
            "FROM market.instruments i "
            "JOIN validation.index_membership m "
            "  ON m.ticker = i.symbol AND m.index_code = :ic "
            "WHERE i.is_active AND i.market = 'US' "
            "  AND i.instrument_type = 'stock' AND NOT m.is_active_now "
            "GROUP BY i.symbol ORDER BY i.symbol"),
            {"ic": index_code}).mappings().all()
        for r in drop_rows:
            if int(str(r["n"])) > 1:
                excluded.append((str(r["symbol"]), ("ambiguous-instrument",)))
            else:
                to_deactivate.append(str(r["symbol"]))

    return UniversePlan(
        mode=mode, to_activate=tuple(to_activate),
        to_deactivate=tuple(to_deactivate),
        already_active=tuple(already_active),
        excluded=tuple(sorted(excluded)), current_members=len(by_ticker),
        fresh_ref=fresh_ref, max_stale_sessions=max_stale_sessions,
        threshold=threshold)


def update_manifest(session: Session, path: Path, *, add: Sequence[str],
                    remove: Sequence[str]) -> tuple[tuple[str, ...],
                                                    tuple[str, ...]]:
    """Extend/prune seeds/universe.json to match activation reality and
    rewrite it deterministically sorted by (symbol, exchange). Entries carry
    exactly sync_universe's REQUIRED_FIELDS, sourced from the instrument row;
    a missing sector or instrument_type refuses (fail closed) — the manifest
    must never carry a hole sync_universe would faithfully propagate."""
    entries = json.loads(path.read_text())
    if not isinstance(entries, list):
        raise ValueError(f"universe manifest {path} must be a JSON array")
    present = {(e["symbol"], e["exchange"]) for e in entries}
    added: list[str] = []
    for sym in sorted(set(add)):
        rows = session.execute(text(
            "SELECT symbol, exchange, market, instrument_type, name, "
            "       sector_gics, currency, economic_exposure "
            "FROM market.instruments WHERE symbol = :s AND market = 'US'"),
            {"s": sym}).mappings().all()
        if len(rows) != 1:
            raise ValueError(f"{sym}: expected exactly one market='US' "
                             f"instrument row, found {len(rows)}")
        r = rows[0]
        if (r["symbol"], r["exchange"]) in present:
            continue
        if r["sector_gics"] in (None, ""):
            raise ValueError(f"{sym}: missing sector_gics — refusing a "
                             "manifest entry with a hole (run backfill_gics)")
        if not r["instrument_type"]:
            raise ValueError(f"{sym}: missing instrument_type")
        entry = {"symbol": r["symbol"], "exchange": r["exchange"],
                 "market": r["market"], "instrument_type": r["instrument_type"],
                 "name": r["name"] or r["symbol"],
                 "sector_gics": r["sector_gics"],
                 "currency": str(r["currency"]).strip(),
                 "economic_exposure": list(r["economic_exposure"] or ["US"])}
        assert tuple(entry) == MANIFEST_FIELDS  # shape pinned to the contract
        entries.append(entry)
        present.add((entry["symbol"], entry["exchange"]))
        added.append(sym)

    remove_set = set(remove)
    removed = sorted({e["symbol"] for e in entries
                      if e["symbol"] in remove_set and e["market"] == "US"
                      and e["instrument_type"] == "stock"})
    entries = [e for e in entries
               if not (e["symbol"] in remove_set and e["market"] == "US"
                       and e["instrument_type"] == "stock")]

    entries.sort(key=lambda e: (str(e["symbol"]), str(e["exchange"])))
    path.write_text(json.dumps(entries, indent=2) + "\n")
    return tuple(added), tuple(removed)


def apply_plan(session: Session, plan: UniversePlan, *,
               audit: PostgresAuditLog, seeds_path: Path,
               sanity_min: int = SANITY_MIN, sanity_max: int = SANITY_MAX,
               max_changes: int = RECONCILE_MAX_CHANGES,
               index_code: str = INDEX_CODE) -> AppliedUniverseChange:
    """Execute a plan: guards FIRST (no partial writes on refusal), then the
    is_active flips (rowcounts verified against the plan — concurrent drift
    aborts the transaction), then the manifest, then exactly ONE audit event."""
    if plan.mode == "activate":
        n = len(plan.to_activate)
        if not sanity_min <= n <= sanity_max:
            raise ValueError(
                f"REFUSING to activate {n} names: outside the sanity band "
                f"[{sanity_min}, {sanity_max}] (fat-finger guard). If this "
                "count is genuinely right, change the band constant in a "
                "reviewed commit — there is no bypass flag.")
    else:
        n_changes = len(plan.to_activate) + len(plan.to_deactivate)
        if n_changes > max_changes:
            raise ValueError(
                f"REFUSING to reconcile {n_changes} flips: over the drift cap "
                f"({max_changes}). Semi-annual S&P churn should be far "
                "smaller — check the membership snapshot before proceeding.")

    if plan.to_activate:
        res = session.execute(text(
            "UPDATE market.instruments SET is_active = TRUE "
            "WHERE market = 'US' AND NOT is_active "
            "  AND symbol = ANY(:syms)"), {"syms": list(plan.to_activate)})
        if res.rowcount != len(plan.to_activate):
            raise RuntimeError(
                f"activation flipped {res.rowcount} rows, plan expected "
                f"{len(plan.to_activate)} — instruments changed under us; "
                "aborting (transaction rolls back)")
    if plan.to_deactivate:
        res = session.execute(text(
            "UPDATE market.instruments SET is_active = FALSE "
            "WHERE market = 'US' AND is_active AND instrument_type = 'stock' "
            "  AND symbol = ANY(:syms)"), {"syms": list(plan.to_deactivate)})
        if res.rowcount != len(plan.to_deactivate):
            raise RuntimeError(
                f"deactivation flipped {res.rowcount} rows, plan expected "
                f"{len(plan.to_deactivate)} — aborting (transaction rolls "
                "back)")

    seeds_added, seeds_removed = update_manifest(
        session, seeds_path, add=plan.to_activate, remove=plan.to_deactivate)

    event_type = ("market.universe.activated" if plan.mode == "activate"
                  else "market.universe.reconciled")
    guard: dict[str, object] = (
        {"sanity_band": [sanity_min, sanity_max]} if plan.mode == "activate"
        else {"max_changes": max_changes})
    audit.append(
        event_type=event_type, entity_type="market", entity_id="universe",
        actor_type="human", actor_id="activate_universe",
        payload={"index_code": index_code, "mode": plan.mode,
                 "activated": list(plan.to_activate),
                 "activated_count": len(plan.to_activate),
                 "deactivated": list(plan.to_deactivate),
                 "deactivated_count": len(plan.to_deactivate),
                 "already_active_count": len(plan.already_active),
                 "excluded": {s: list(r) for s, r in plan.excluded},
                 "excluded_count": len(plan.excluded),
                 "current_members": plan.current_members,
                 "fresh_ref": plan.fresh_ref.isoformat(),
                 "max_stale_sessions": plan.max_stale_sessions,
                 "freshness_threshold": plan.threshold.isoformat(),
                 **guard,
                 "seeds_path": str(seeds_path),
                 "seeds_added": list(seeds_added),
                 "seeds_removed": list(seeds_removed),
                 "note": "deactivation only stops new signals/ingest; open "
                         "positions exit via the normal paths"})
    return AppliedUniverseChange(
        event_type=event_type, activated=plan.to_activate,
        deactivated=plan.to_deactivate, seeds_added=seeds_added,
        seeds_removed=seeds_removed)


def _print_plan(plan: UniversePlan, *, sanity_min: int, sanity_max: int,
                max_changes: int) -> None:
    print(f"universe plan (mode={plan.mode}): current members "
          f"{plan.current_members}, already active "
          f"{len(plan.already_active)}, would activate "
          f"{len(plan.to_activate)}, would deactivate "
          f"{len(plan.to_deactivate)}, excluded {len(plan.excluded)}")
    print(f"freshness: last bar >= {plan.threshold} "
          f"({plan.max_stale_sessions} XNYS sessions before {plan.fresh_ref})")
    by_reason: dict[str, list[str]] = {}
    for sym, reasons in plan.excluded:
        for reason in reasons:
            by_reason.setdefault(reason, []).append(sym)
    for reason in sorted(by_reason):
        syms = by_reason[reason]
        print(f"  excluded [{reason}] x{len(syms)}: {', '.join(syms)}")
    if plan.to_deactivate:
        print(f"  would deactivate: {', '.join(plan.to_deactivate)} "
              "(positions untouched; exits via normal paths)")
    if plan.mode == "activate":
        n = len(plan.to_activate)
        ok = sanity_min <= n <= sanity_max
        print(f"sanity band [{sanity_min}, {sanity_max}]: "
              f"{'OK — --apply would proceed' if ok else f'VIOLATED — --apply would REFUSE ({n})'}")
    else:
        n = len(plan.to_activate) + len(plan.to_deactivate)
        ok = n <= max_changes
        print(f"drift cap {max_changes}: "
              f"{'OK — --apply would proceed' if ok else f'EXCEEDED — --apply would REFUSE ({n})'}")


def main() -> int:
    from atlas.core.clock import SystemClock
    from atlas.core.db import session_scope

    p = argparse.ArgumentParser(
        description="ADR-0016 universe activation / semi-annual membership "
                    "reconcile. Dry-run by default; --apply flips is_active, "
                    "audits ONE event, and extends seeds/universe.json.")
    p.add_argument("--apply", action="store_true",
                   help="execute the plan (default: dry-run, writes nothing)")
    p.add_argument("--reconcile", action="store_true",
                   help="drift mode: also deactivate members no longer in "
                        "the index (ADR-0016 decision 4)")
    p.add_argument("--fresh-ref", type=date.fromisoformat, default=FRESH_REF,
                   help="freshness anchor (default: the PIT backfill end "
                        f"{FRESH_REF}); pass the last completed session on "
                        "later reconcile runs")
    p.add_argument("--seeds", type=Path, default=DEFAULT_SEEDS)
    p.add_argument("--index", default=INDEX_CODE)
    a = p.parse_args()

    mode = "reconcile" if a.reconcile else "activate"
    with session_scope() as s:
        plan = build_plan(s, mode=mode, index_code=a.index,
                          fresh_ref=a.fresh_ref)
        _print_plan(plan, sanity_min=SANITY_MIN, sanity_max=SANITY_MAX,
                    max_changes=RECONCILE_MAX_CHANGES)
        if not a.apply:
            print("dry-run: nothing written. Re-run with --apply to execute.")
            return 0
        result = apply_plan(s, plan, audit=PostgresAuditLog(s, SystemClock()),
                            seeds_path=a.seeds, index_code=a.index)
    print(f"APPLIED: activated {len(result.activated)}, deactivated "
          f"{len(result.deactivated)}, manifest +{len(result.seeds_added)}/"
          f"-{len(result.seeds_removed)} ({a.seeds}); audit event "
          f"{result.event_type} appended")
    print("catch-up: the next nightly advances every newly-active name from "
          "its own latest stored bar (no manual backfill; gates go RED on "
          "anything the vendor cannot serve)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
