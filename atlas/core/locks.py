"""Canonical advisory-lock ordering (F-018).

Two transaction-scoped advisory locks can deadlock if acquired in opposite orders
(ABBA): the daily cycle holds the audit-chain lock (from its first audit append)
and later takes the trading-lifecycle lock, while an API approval takes the
lifecycle lock first and then appends audit. Postgres detects the deadlock and
aborts one transaction — which, for the atomic daily cycle, throws the day away.

The fix is a single GLOBAL ACQUISITION ORDER. Every path that needs more than one
of these locks acquires them in this fixed rank order, so a cycle is impossible:

    rank 1  AUDIT_LOCK            (audit.decision_events chain)
    rank 2  TRADING_LIFECYCLE     (book-mutating proposal/order lifecycle)
    rank 3  factory family lock   (recipe registration; acquired alone)

`pg_advisory_xact_lock` is re-entrant within a session, so re-acquiring a lock a
caller already holds (e.g. the audit lock, later taken again by an append) is a
harmless no-op.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

# rank 1 — the audit-chain serialisation lock (mirrors atlas.core.audit_repo).
AUDIT_LOCK = 762001
# rank 2 — the portfolio-wide book-lifecycle lock (a named hashtext key).
TRADING_LIFECYCLE_LOCK_NAME = "atlas.trading.lifecycle"


def acquire_audit_lock(session: Session) -> None:
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": AUDIT_LOCK})


def acquire_trading_lifecycle_lock(session: Session) -> None:
    """Acquire the book-lifecycle lock in canonical order: the audit lock (rank 1)
    is taken FIRST so this path can never form an ABBA cycle with the daily cycle
    or an audit append. Re-entrant, so the later audit append is a no-op."""
    acquire_audit_lock(session)
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:n))"),
        {"n": TRADING_LIFECYCLE_LOCK_NAME})
