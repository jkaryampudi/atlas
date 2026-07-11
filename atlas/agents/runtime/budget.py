"""Daily cost circuit breaker (Doc 01 §5, Constitution 5.4). DB-backed counter."""
from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session


class BudgetExhausted(Exception):
    pass


def spend_and_check(session: Session, *, cost_usd: float,
                    daily_cap_usd: float, day: date | None = None) -> float:
    """Adds cost to today's tally; raises if the cap is breached. Returns total."""
    if day is None:
        total = session.execute(text(
            "SELECT COALESCE(SUM(cost_usd),0) FROM research.agent_runs "
            "WHERE created_at::date = CURRENT_DATE")).scalar() or 0
    else:
        total = session.execute(text(
            "SELECT COALESCE(SUM(cost_usd),0) FROM research.agent_runs "
            "WHERE created_at::date = :d"), {"d": day}).scalar() or 0
    new_total = float(total) + cost_usd
    if new_total > daily_cap_usd:
        raise BudgetExhausted(
            f"daily LLM budget breached: {new_total:.2f} > {daily_cap_usd:.2f} USD")
    return new_total
