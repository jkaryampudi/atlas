"""Operator alerting: a failure must find the Principal, not wait to be found.

Transport is a plain ntfy-style webhook (POST body = message, Title header):
set ATLAS_ALERT_URL to e.g. https://ntfy.sh/<your-private-topic> and install
the ntfy app, or point it at any webhook receiver. With no URL configured,
alerts degrade to stderr — visible in the launchd log, never lost silently,
never an exception: alerting failures must not take the pipeline down with
them (the pipeline's own exit code is the ground truth the scheduler sees).

URGENT alerts (ops-reliability build, 2026-07): alert_urgent() is notify()
plus a RECORD — one ops.alert.urgent audit event per (kind, key), which is
both the once-only dedupe latch (the CUSUM pattern: an existing event
suppresses a re-page) and the guarantee that an alert LANDS somewhere even
with ATLAS_ALERT_URL unset — the event is what the morning brief reads.
The three wired conditions, each fixing an OBSERVED failure:

  * proposal_expiring — a pending_approval proposal inside its final
    EXPIRING_SOON window (default 6h). Once per proposal, keyed on its id.
    (Core proposals silently expired unapproved twice; nobody was paged.)
  * billing_outage — the desk died with a NON-TRANSIENT client HTTP error
    (4xx, not 429: exactly the class runner.py's classification propagates
    raw — the vendor's 400 "credit balance too low" signature) after ZERO
    completed LLM calls today. Once per day, keyed on the date. (FOUR
    billing outages in five days produced silent $0.00 desk nights.)
  * cycle-node failures and band/CUSUM breaches already page: t9 folds every
    fail-soft node into one high-priority notify, the scheduler pages any
    non-zero cycle exit, and bands.py notifies demotions/CUSUM breaches with
    their own audit events. Those paths stand; the brief reads their records.
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:  # heavy imports stay lazy: notify() must work anywhere
    from sqlalchemy.orm import Session

    from atlas.core.clock import Clock

_TIMEOUT = 10.0

URGENT_EVENT = "ops.alert.urgent"
EXPIRING_SOON = timedelta(hours=6)   # final-approach window for a pending proposal


def notify(title: str, message: str, *, priority: str = "default") -> bool:
    """Best-effort push to the operator. Returns True only on confirmed
    delivery; NEVER raises."""
    url = os.environ.get("ATLAS_ALERT_URL", "").strip()
    line = f"[atlas-alert] {title}: {message}"
    if not url:
        print(line + " (ATLAS_ALERT_URL unset — stderr only)", file=sys.stderr)
        return False
    try:
        r = httpx.post(url, content=message.encode(),
                       headers={"Title": title, "Priority": priority},
                       timeout=_TIMEOUT)
        if r.status_code // 100 == 2:
            return True
        print(f"{line} (webhook {r.status_code})", file=sys.stderr)
        return False
    except Exception as e:  # noqa: BLE001 — alerting must never crash the caller
        print(f"{line} (webhook unreachable: {e})", file=sys.stderr)
        return False


def alert_urgent(session: Session, clock: Clock, *, kind: str, key: str, title: str,
                 message: str, priority: str = "high") -> bool:
    """notify() + one ops.alert.urgent audit event per (kind, key) — the event
    is BOTH the once-only latch and the durable record (module docstring).
    Returns True when this call emitted (False = latched earlier, no re-page).
    With ATLAS_ALERT_URL unset the push degrades to stderr but the event still
    lands: unset transport must never mean an unrecorded condition."""
    from sqlalchemy import text

    from atlas.core.audit_repo import PostgresAuditLog

    entity_id = f"{kind}:{key}"
    already = session.execute(text(
        "SELECT 1 FROM audit.decision_events "
        "WHERE event_type = :et AND entity_id = :eid LIMIT 1"),
        {"et": URGENT_EVENT, "eid": entity_id}).first()
    if already is not None:
        return False
    delivered = notify(title, message, priority=priority)
    PostgresAuditLog(session, clock).append(
        event_type=URGENT_EVENT, entity_type="ops_alert", entity_id=entity_id,
        actor_type="dcp", actor_id="ops_alerts",
        payload={"kind": kind, "key": key, "title": title, "message": message,
                 "priority": priority, "delivered": delivered})
    return True


def check_expiring_proposals(session: Session, clock: Clock, *,
                             within: timedelta = EXPIRING_SOON) -> tuple[str, ...]:
    """Page once per pending_approval proposal that enters its final `within`
    window (not yet expired — t2's expire_stale owns the funeral). Keyed on
    the proposal id, so the daily cycle AND any out-of-band run (cron/manual)
    can both call this without double-paging. Returns the ids that fired NOW."""
    from sqlalchemy import text

    now = clock.now()
    rows = session.execute(text(
        "SELECT tp.id, tp.action, tp.origin, tp.position_size, tp.expires_at, "
        "       i.symbol "
        "FROM trading.trade_proposals tp "
        "LEFT JOIN market.instruments i ON i.id = tp.instrument_id "
        "WHERE tp.state = 'pending_approval' "
        "  AND tp.expires_at > :now AND tp.expires_at <= :soon "
        "ORDER BY tp.expires_at"),
        {"now": now, "soon": now + within}).all()
    fired: list[str] = []
    for r in rows:
        hours_left = (r.expires_at - now).total_seconds() / 3600
        if alert_urgent(
                session, clock, kind="proposal_expiring", key=str(r.id),
                title=f"Atlas: proposal expiring in {hours_left:.1f}h — "
                      f"{r.symbol or '?'} still awaits your seal",
                message=(f"{r.origin} {r.action} {int(r.position_size)} "
                         f"{r.symbol or '?'} (proposal {r.id}) expires at "
                         f"{r.expires_at.isoformat()} — approve or reject on "
                         "the console, or it dies unactioned"),
                priority="high"):
            fired.append(str(r.id))
    return tuple(fired)


def is_billing_outage_error(exc: BaseException) -> bool:
    """True iff `exc` (or its __cause__/__context__ chain) is a NON-TRANSIENT
    client HTTP error — 4xx and not 429. This mirrors runner.py's failure
    classification exactly: 429/5xx/timeouts are retried and surface as
    TransientLlmFailure; every other 4xx is 'a configuration bug and
    propagates raw'. The vendor's credit-exhaustion 400 takes that raw path,
    which is why it reaches the desk as an unclassified exception."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, httpx.HTTPStatusError):
            code = cur.response.status_code
            return 400 <= code < 500 and code != 429
        cur = cur.__cause__ or cur.__context__
    return False


def maybe_billing_outage_alert(session: Session, clock: Clock, *,
                               exc: BaseException) -> bool:
    """The billing-outage detector (module docstring): fires ONE high-priority
    page per day iff the desk's failure carries the non-transient-client-error
    signature AND zero LLM calls completed today (research.agent_runs is
    empty for the date — a completed call, even a schema_fail or budget_kill,
    persists a row; a never-completed 400 persists nothing). Partial days
    (some calls landed, then the failure) and transient outages (429/5xx —
    typed by the desk, never raised raw) are NOT billing outages."""
    from sqlalchemy import text

    if not is_billing_outage_error(exc):
        return False
    day = clock.now().date()
    n_runs = session.execute(text(
        "SELECT count(*) FROM research.agent_runs "
        "WHERE created_at::date = :d"), {"d": day}).scalar_one()
    if int(n_runs) > 0:
        return False   # partial day: the vendor completed calls — not an outage
    return alert_urgent(
        session, clock, kind="billing_outage", key=day.isoformat(),
        title="Atlas: API credits exhausted — desk skipped",
        message=(f"{day}: every LLM call failed with a non-transient client "
                 f"error ({str(exc)[:200]}) and zero calls completed — the "
                 "400-credit signature. The desk produced nothing tonight; "
                 "top up the vendor account. (Fourth-in-five-days class of "
                 "outage this detector exists for.)"),
        priority="high")


def main() -> None:
    """Out-of-band expiring-proposal sweep — schedule HOURLY via cron/launchd
    (`python -m atlas.ops.alerts`). The daily cycle's t9b sweeps too, but a
    24h agent proposal crosses its final 6h BETWEEN cycles; an intra-day
    sweep is what actually catches it before t2 buries it. Once-per-proposal
    latching makes any schedule safe — hourly runs never re-page."""
    from atlas.core.clock import SystemClock
    from atlas.core.db import session_scope

    with session_scope() as s:
        fired = check_expiring_proposals(s, SystemClock())
    print(f"expiring-proposal sweep: paged {len(fired)} proposal(s)")


if __name__ == "__main__":
    main()
