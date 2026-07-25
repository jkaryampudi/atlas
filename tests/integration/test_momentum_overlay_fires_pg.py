"""F-011 regression: the §12 momentum-factor overlay must actually fire.

A surviving mutation-test artifact filtered momentum attribution on
`st.state IN ('MUTANT_no_such_state')` — a value the CHECK constraint forbids —
so `_proposal_is_momentum` / `_momentum_symbols` were structurally always empty
and the overlay never bound. These tests prove: (a) a real momentum signal from
a PAPER strategy is attributed as momentum; (b) the same signal from a
research_shadow strategy is NOT (authoritative states only); (c) the old
'MUTANT_no_such_state' filter would have returned False for the paper case.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.trading.proposals import _momentum_symbols, _proposal_is_momentum
from tests.conftest import requires_pg

pytestmark = requires_pg
T0 = datetime(2026, 7, 21, 20, 0, tzinfo=UTC)


def _seed_momentum_signal(s, state: str) -> str:
    """A xsmom-pit-tr (MOMENTUM_FAMILIES) strategy in `state` with one signal on
    ZMOM. Returns the signal id."""
    clock = FrozenClock(T0)
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type,"
        " name, currency) VALUES ('ZMOM','XTEST','US','stock','ZMOM','USD') "
        "RETURNING id")).scalar()
    sid = s.execute(text(
        "INSERT INTO quant.strategies (family,name,version,spec,code_sha,"
        " tolerance_bands,state) VALUES ('xsmom-pit-tr','x','1.0.0','{}','s','{}',"
        " :st) RETURNING id"), {"st": state}).scalar()
    sig = s.execute(text(
        "INSERT INTO quant.signals (strategy_id, instrument_id, signal_date, "
        " direction, rank, formation_return, valid_until, created_at) "
        "VALUES (:sid,:iid,'2026-07-21','long',1,0.5,'2026-08-31',:ca) "
        "RETURNING id"), {"sid": sid, "iid": iid, "ca": clock.now()}).scalar()
    return str(sig)


def test_paper_momentum_signal_is_attributed(clean_audit):
    s = clean_audit
    sig = _seed_momentum_signal(s, "paper")
    assert _proposal_is_momentum(s, [sig]) is True          # F-011: overlay fires
    assert "ZMOM" not in _momentum_symbols(s)  # no OPEN lot yet — symbol set is lot/live-proposal based


def test_research_shadow_momentum_signal_is_not_attributed(clean_audit):
    """Authoritative states only: a shadow strategy deploys no capital, so its
    signal must not count toward the momentum-factor cap."""
    s = clean_audit
    sig = _seed_momentum_signal(s, "research_shadow")
    assert _proposal_is_momentum(s, [sig]) is False


def test_old_mutant_filter_would_have_missed_the_paper_signal(clean_audit):
    """Proves the pre-remediation defect: the 'MUTANT_no_such_state' filter
    returns no rows even for a paper momentum signal."""
    s = clean_audit
    sig = _seed_momentum_signal(s, "paper")
    old = s.execute(text(
        "SELECT 1 FROM quant.signals sg "
        "JOIN quant.strategies st ON st.id = sg.strategy_id "
        "WHERE sg.id = ANY(:ids) AND st.family = ANY(:fams) "
        "  AND st.state IN ('MUTANT_no_such_state') LIMIT 1"),
        {"ids": [sig], "fams": ["xsmom-pit-tr"]}).first()
    assert old is None                                       # the defect
    # the remediated predicate sees it
    assert _proposal_is_momentum(s, [sig]) is True
