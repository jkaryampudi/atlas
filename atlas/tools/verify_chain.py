"""Nightly audit-chain verification (task 2; Doc 05 §6, P1 exit criterion).

Re-walks the full audit.decision_events hash chain. Any tamper, deletion, or
fork breaks a link and the job exits non-zero with the breach on stderr — wire
it to cron/launchd nightly and alert on a non-zero exit:

    make verify-chain            # or: python -m atlas.tools.verify_chain
    # crontab: 0 3 * * *  cd <repo> && make verify-chain || <alert>

A verification pass is itself a material action and is appended to the chain
(actor 'scheduler'), so silent gaps in verification are visible in the log.
"""
from __future__ import annotations

import sys

from atlas.core.audit import ChainVerificationError
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock, SystemClock
from atlas.core.db import session_scope
from sqlalchemy.orm import Session


def run(session: Session, clock: Clock) -> int:
    """Verify the chain and append the verification event. Returns the count of
    verified events; raises ChainVerificationError on any break."""
    log = PostgresAuditLog(session, clock)
    n = log.verify()
    log.append(event_type="audit.chain.verified", entity_type="audit",
               entity_id="decision_events", actor_type="scheduler",
               actor_id="verify_chain", payload={"verified_events": n})
    return n


def main() -> int:
    with session_scope() as s:
        try:
            n = run(s, SystemClock())
        except ChainVerificationError as e:
            print(f"AUDIT CHAIN VERIFICATION FAILED: {e}", file=sys.stderr)
            print("Do not trust downstream state until resolved (Doc 08 kill "
                  "condition: verification failure => halt + post-mortem).",
                  file=sys.stderr)
            return 1
    print(f"audit chain OK: {n} event(s) verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
