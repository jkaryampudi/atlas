"""Downgrade xsmom-pit-tr from paper (APPROVED) to research_shadow (ADR-0018) —
one-shot, audited tool.

The 2026-07-20 independent review (REVIEW_PACKAGE/) returned "REJECT STRATEGY
EVIDENCE" for the executable momentum sleeve: the deployed generator ranks
split-adjusted PRICE return while approval validated TOTAL return (deployed
signal != validated signal); the honest lineage-count DSR (~0.85, ADR-0016) is
below the 0.90 bar; and the run is not reproducible from an immutable code+data
snapshot. Phase P0 moves the strategy to a NON-AUTHORITATIVE 'research_shadow'
status: it deploys no paper capital (the bridge fail-closed guard blocks any
proposal from its signals) and its performance is never reported as validated,
but its identity and history are preserved for observation.

This does NOT touch strategy math, parameters, the sleeve fraction, backtest
results, or any historical file. It flips the row's state and stamps shadowed_at
(the fail-closed promotion gate then requires a NEW signed validation artifact
created after this moment before the strategy could ever return to paper). The
transition is a material action and lands on the append-only audit chain.

Usage (deliberate, spelled-out flags — no silent defaults):

    python -m atlas.tools.downgrade_xsmom_shadow \
        --downgraded-by "Jay Karyampudi (Principal)" \
        --decision-ref ADR-0018 \
        --review-ref REVIEW_PACKAGE/FINAL_INDEPENDENT_REVIEW_READINESS.md
"""
from __future__ import annotations

import argparse

from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import SystemClock
from atlas.core.db import session_scope
from atlas.dcp.strategy_lifecycle import AUTHORITATIVE_STATES, RESEARCH_SHADOW

FAMILY = "xsmom-pit-tr"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--downgraded-by", required=True,
                    help="the human (Principal) recording the downgrade")
    ap.add_argument("--decision-ref", required=True,
                    help="the durable decision record, e.g. ADR-0018")
    ap.add_argument("--review-ref", required=True,
                    help="the independent-review verdict this acts on")
    ap.add_argument("--family", default=FAMILY)
    a = ap.parse_args(argv)

    clock = SystemClock()
    with session_scope() as s:
        audit = PostgresAuditLog(s, clock)
        row = s.execute(text(
            "SELECT id, name, version, state FROM quant.strategies "
            "WHERE family = :f"), {"f": a.family}).mappings().first()
        if row is None:
            print(f"REFUSED: no quant.strategies row for family '{a.family}'")
            return 1
        if row["state"] == RESEARCH_SHADOW:
            print(f"NO-OP: {a.family} is already 'research_shadow'")
            return 0
        if row["state"] not in AUTHORITATIVE_STATES:
            print(f"REFUSED: {a.family} is '{row['state']}', not paper/live — "
                  "this tool only downgrades an authoritative sleeve")
            return 1

        updated = s.execute(text(
            "UPDATE quant.strategies SET state = :new, shadowed_at = :ts "
            "WHERE id = :sid AND state IN ('paper','live') RETURNING id"),
            {"new": RESEARCH_SHADOW, "ts": clock.now(),
             "sid": row["id"]}).first()
        if updated is None:                       # concurrent state change
            print(f"REFUSED: {a.family} state changed under us — no write")
            return 1

        audit.append(
            event_type="quant.strategy.research_shadow", entity_type="strategy",
            entity_id=str(row["id"]), actor_type="human",
            actor_id=a.downgraded_by,
            payload={"family": a.family, "name": row["name"],
                     "version": row["version"], "old_state": row["state"],
                     "new_state": RESEARCH_SHADOW,
                     "decision_ref": a.decision_ref,
                     "review_ref": a.review_ref,
                     "reason": "independent review REJECT STRATEGY EVIDENCE — "
                               "deployed signal != validated signal; DSR ~0.85 "
                               "at lineage count; not reproducible (ADR-0018)"})
        print(f"DOWNGRADED: {a.family}/{row['name']} v{row['version']} "
              f"'{row['state']}' -> '{RESEARCH_SHADOW}' (non-authoritative). "
              "Deploys no capital; re-promotion requires a NEW signed "
              "validation artifact (ADR-0018 fail-closed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
