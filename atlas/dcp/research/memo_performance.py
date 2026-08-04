"""Live (unrealized) read of the desk's BUY calls vs SPY since each memo.

An informal, always-current answer to "how are the desk's recommendations
doing?" — computed on the fund's AUD total-return reporting basis from the SAME
services the scorecard uses (F-006), so this preview and the eventual verdict
can never diverge on basis. NOT the verdict: the scorecard's fixed 20/60-session
outcomes (dartboard-baselined, write-once) are the only decision-grade grades.
Fail-closed per symbol: an unpriceable series reports excess=None, never a guess.
Shadow-run memos are excluded (non-actionable by definition, ADR-0018 labelling).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.clock import Clock
from atlas.dcp.market_data.benchmark import (benchmark_reporting_series,
                                             reporting_close_series)
from atlas.dcp.scorecard import anchor_index


@dataclass(frozen=True)
class OpenCall:
    symbol: str
    memo_date: date
    conviction: str | None
    sessions: int              # priceable sessions elapsed since the memo anchor
    excess: float | None       # unrealized excess vs SPY; None = unpriceable


def open_buy_calls(session: Session, clock: Clock) -> list[OpenCall]:
    """One row per (symbol, memo day) BUY memo, newest first. `excess` is the
    symbol's return minus SPY's over the same anchored window, both on the AUD
    total-return reporting basis; `sessions` shows how seasoned the reading is
    (small = noise, the caller labels it honestly)."""
    on = clock.now().date()
    try:
        spy = sorted(benchmark_reporting_series(session, through=on).items())
    except RuntimeError:                    # benchmark unpriceable -> fail closed
        spy = []
    spy_dates = [d for d, _ in spy]

    rows = session.execute(text(
        "SELECT DISTINCT ON (m.instrument_symbol, m.created_at::date) "
        "       m.instrument_symbol AS sym, m.created_at::date AS d0, "
        "       m.conviction, i.id AS iid, i.currency AS currency "
        "FROM research.memos m "
        "LEFT JOIN research.agent_runs r ON r.id = m.agent_run_id "
        "LEFT JOIN market.instruments i ON i.symbol = m.instrument_symbol "
        "WHERE m.recommendation = 'BUY' AND COALESCE(r.shadow, false) = false "
        "ORDER BY m.instrument_symbol, m.created_at::date, m.created_at DESC")).all()

    out: list[OpenCall] = []
    for r in rows:
        excess: float | None = None
        sessions = 0
        if r.iid is not None and spy:
            try:
                series = sorted(reporting_close_series(
                    session, instrument_id=r.iid, symbol=r.sym,
                    currency=r.currency, through=on).items())
            except RuntimeError:            # unpriceable in AUD -> fail closed
                series = []
            dates = [d for d, _ in series]
            a = anchor_index(dates, r.d0)
            sa = anchor_index(spy_dates, r.d0)
            if (a is not None and sa is not None and len(series) - 1 > a
                    and len(spy) - 1 > sa
                    and series[a][1] > 0 and spy[sa][1] > 0):
                sessions = min(len(series) - 1 - a, len(spy) - 1 - sa)
                px_ret = series[a + sessions][1] / series[a][1] - 1
                spy_ret = spy[sa + sessions][1] / spy[sa][1] - 1
                excess = float(px_ret - spy_ret)
        out.append(OpenCall(symbol=str(r.sym), memo_date=r.d0,
                            conviction=r.conviction, sessions=sessions,
                            excess=excess))
    out.sort(key=lambda c: (c.memo_date, c.symbol), reverse=True)
    return out
