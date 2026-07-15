"""Seed limit_set_v2 (ADR-0014, option B, signed) into risk.limit_sets — one-shot.

ADR-0014 authorises the passive index core to deploy at full size (core 70% =
SPY 55% + INDA 15%, satellite 20%, cash 10%). That needs a NEW signed limit set:
limit_set_v1 (ADR-0001) caps EVERY ETF at 15% (L2), so a 55% SPY core leg fails
L2 and lands 'rejected' (documented in core_allocation.py). This tool writes the
Principal-signed successor.

DERIVED, NOT RESTATED. v2 inherits EVERY v1 limit verbatim from the active v1 row
and changes ONLY the three keys ADR-0014's decision names:
  * L5_min_cash_reserve  0.20 -> 0.10  (cash floor drops from 20% to 10%);
  * ADD L2_core_index_etf_weight 0.60  (the index-core ETF CLASS cap);
  * ADD core_index_etf_allowlist ["SPY","INDA"]  (exactly which ETFs get 0.60).
L2_max_etf_weight stays 0.15 — every OTHER ETF keeps the ordinary single cap, and
the engine's L2 (limits.l2_cap_for) applies 0.60 ONLY to an allowlisted symbol.
L7 is unchanged (its core-awareness is the engine change, not a limit value); L9
is unchanged (batched-rebalance semantics belong to the rebalancer, not a limit).

GOVERNANCE. The row supersedes v1 (risk.limit_sets.supersedes is an int column, so
it carries v1's VERSION — the schema cannot hold v1's uuid id) and records the
approver + decision ref in created_by. Single confirmation is permitted: the
dual_confirm_gap CHECK only enforces the >=1h timing gap WHEN confirmation_b is
set, so confirmation_a is stamped from the injected clock and confirmation_b stays
NULL. effective_from is the injected-clock date, so v2 becomes the active set the
day it is signed (load_active_limit_set orders by version desc). Every material
action emits an audit event (invariant 4).

The tool PRINTS the new id and REFUSES (exit 1) if a v2 already exists — limit-set
approvals are not re-runnable. Run it ONLY after the L7 adversarial review passes;
never against dev casually. Requires the approver and the decision reference to be
spelled out, no defaults that could sign silently:

    python -m atlas.tools.seed_limit_set_v2 \
        --approved-by "Jay Karyampudi (Principal)" --decision-ref ADR-0014
"""
from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock, SystemClock
from atlas.core.db import session_scope

V1_VERSION = 1
V2_VERSION = 2


def v2_limits(v1_limits: dict[str, Any]) -> dict[str, Any]:
    """v1's limits with ONLY the ADR-0014 deltas applied. Everything else is
    inherited exactly — the caller passes the active v1 limits dict."""
    limits = dict(v1_limits)
    limits["L5_min_cash_reserve"] = 0.10                 # 0.20 -> 0.10
    limits["L2_core_index_etf_weight"] = 0.60            # new index-core ETF cap
    limits["core_index_etf_allowlist"] = ["SPY", "INDA"]  # who gets 0.60
    return limits


def seed_limit_set_v2(session: Session, clock: Clock, *, approved_by: str,
                      decision_ref: str) -> str:
    """Insert risk.limit_sets v2, derived from the active v1. Returns the new id.

    Refuses (RuntimeError) if a v2 already exists (approvals are not re-runnable)
    or if there is no v1 to supersede. Single-confirm: confirmation_a stamped from
    the injected clock, confirmation_b NULL. Emits an audit event (invariant 4).
    """
    existing = session.execute(text(
        "SELECT id FROM risk.limit_sets WHERE version = :v"),
        {"v": V2_VERSION}).first()
    if existing is not None:
        raise RuntimeError(
            f"REFUSED: risk.limit_sets already has version {V2_VERSION} "
            f"(id {existing.id}) — limit-set approvals are not re-runnable; "
            "supersede it with a new signed version instead")
    v1 = session.execute(text(
        "SELECT version, mode, limits FROM risk.limit_sets WHERE version = :v"),
        {"v": V1_VERSION}).first()
    if v1 is None:
        raise RuntimeError(
            f"REFUSED: no limit_set v{V1_VERSION} to supersede — seed v1 first")
    v1_limits = v1.limits if isinstance(v1.limits, dict) else json.loads(v1.limits)

    now = clock.now()
    created_by = f"{approved_by} ({decision_ref})"
    new_id = session.execute(text(
        "INSERT INTO risk.limit_sets (version, mode, limits, effective_from, "
        " created_by, confirmation_a, confirmation_b, supersedes) "
        "VALUES (:v, :m, CAST(:l AS jsonb), :ef, :cb, :ca, NULL, :sup) RETURNING id"),
        {"v": V2_VERSION, "m": v1.mode, "l": json.dumps(v2_limits(v1_limits)),
         "ef": now.date(), "cb": created_by, "ca": now,
         "sup": v1.version}).scalar_one()

    PostgresAuditLog(session, clock).append(
        event_type="risk.limit_set.created", entity_type="limit_set",
        entity_id=str(new_id), actor_type="human", actor_id=approved_by,
        payload={"limit_set_id": str(new_id), "version": V2_VERSION,
                 "mode": v1.mode, "supersedes_version": v1.version,
                 "decision_ref": decision_ref,
                 "changes": ["L5_min_cash_reserve 0.20->0.10",
                             "+L2_core_index_etf_weight 0.60",
                             "+core_index_etf_allowlist [SPY,INDA]"]})
    return str(new_id)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--approved-by", required=True,
                    help="the Principal's name — recorded in created_by and the "
                         "audit event actor")
    ap.add_argument("--decision-ref", required=True,
                    help="the signed decision reference, e.g. ADR-0014")
    a = ap.parse_args(argv)

    clock = SystemClock()
    with session_scope() as s:
        try:
            new_id = seed_limit_set_v2(
                s, clock, approved_by=a.approved_by, decision_ref=a.decision_ref)
        except RuntimeError as e:
            print(str(e))
            return 1
        print(f"SEEDED limit_set v{V2_VERSION}: id={new_id}")
        print(f"  approved_by: {a.approved_by}  ref: {a.decision_ref}")
        print(f"  supersedes: v{V1_VERSION}  (L5 0.20->0.10; L2 core cap 0.60 "
              "for SPY/INDA; L2 other-ETF cap 0.15 unchanged)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
