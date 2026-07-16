"""PEAD/SUE production signal generation (atlas/dcp/signals/pead/generate.py,
ADR-0013/0014) against the isolated satellite test DB.

Hand-built earnings fixture -> SUE ranking -> winner set, the eligibility
gates (price at t, >= 4 priors, staleness), the initiation / month-end /
catch_up triggers under FrozenClock, idempotent re-runs, the STRUCTURAL
no-look-ahead guarantee (a report announced after the signal session cannot
touch the ranking), the desk's PEAD-first shortlist lane and its proposal
exclusion, and the PEAD SIGNALS evidence block.

Golden derivation (signal session 2026-07-15). Every ranked name shares the
same four prior quarterly surprises [2, 4, 2, 4] (sample stdev sd), so SUE
ranks by the CURRENT surprise alone:
  PSGA: current surprise +6 -> SUE +6/sd  -> rank 1
  PSGC: current surprise +4 -> SUE +4/sd  -> rank 2
  PSGB: current surprise +2 -> SUE +2/sd  -> rank 3
  PSGD: current surprise -2 -> SUE -2/sd  -> rank 4
  PSGE: only 3 priors            -> SUE undefined (< 4)     -> ineligible
  PSGF: latest report 2026-01-05 -> stale (> 63 sessions)   -> ineligible
  PSGG: fresh SUE but NO close on 2026-07-15 (not tradable) -> ineligible
n_eligible = 4 -> winner_count(4) = max(10, 0) = 10 -> all four, never padded.
"""
from __future__ import annotations

import json
import statistics
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from atlas.agents.runtime.grounding import corpus_numeric_tokens
from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.signals.pead.generate import (
    active_pead_signal_symbols,
    extract_pead_signal_evidence,
    generate_pead_signals,
)
from tests.conftest import requires_pg

pytestmark = requires_pg

T15 = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)      # after the 7-15 US close
SESSION = date(2026, 7, 15)
MONTH_END = date(2026, 7, 31)
AUG_END = date(2026, 8, 31)
BANDS = {"max_drawdown_from_sleeve_peak": -0.40,
         "trailing_126_session_excess_vs_spy_tr_pp": -25.0,
         "demote_to": "suspended", "provisional": True}

_SD = statistics.stdev([2.0, 4.0, 2.0, 4.0])
# (symbol, rank, current surprise) — the eligible, ranked winner set
GOLDEN = [("PSGA", 1, 6.0), ("PSGC", 2, 4.0), ("PSGB", 3, 2.0), ("PSGD", 4, -2.0)]
EXPECT_SUE = {sym: surp / _SD for sym, _, surp in GOLDEN}

# four prior quarters, shared by every ranked name (surprises [2,4,2,4])
_PRIORS = [(date(2025, 6, 30), date(2025, 7, 20), 2.0),
           (date(2025, 9, 30), date(2025, 10, 20), 4.0),
           (date(2025, 12, 31), date(2026, 1, 20), 2.0),
           (date(2026, 3, 31), date(2026, 4, 20), 4.0)]
_CURRENT_FPE, _CURRENT_RD = date(2026, 6, 30), date(2026, 7, 10)  # knowable by 7-15
# an old prior block for the stale name (its "current" lands 2026-01-05)
_STALE_PRIORS = [(date(2024, 6, 30), date(2024, 7, 20), 2.0),
                 (date(2024, 9, 30), date(2024, 10, 20), 4.0),
                 (date(2024, 12, 31), date(2025, 1, 20), 2.0),
                 (date(2025, 3, 31), date(2025, 4, 20), 4.0)]

_ELIG_DAYS = trading_days_between("US", date(2026, 6, 15), date(2026, 8, 4))
_PSGG_DAYS = trading_days_between("US", date(2026, 6, 15), date(2026, 7, 13))


def _clean(s) -> None:
    s.execute(text("DELETE FROM quant.sleeve_daily"))
    s.execute(text("DELETE FROM quant.signals"))
    s.execute(text("DELETE FROM quant.strategies WHERE family = 'pead-sue-tr'"))
    s.execute(text("DELETE FROM market.earnings_surprises WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'PSG%')"))
    s.execute(text("DELETE FROM trading.trade_proposals WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'PSG%')"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'PSG%')"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol LIKE 'PSG%'"))


def _instrument(s, symbol: str, itype: str = "stock"):
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, sector_gics, currency) "
        "VALUES (:sym, 'XTEST', 'US', :t, :sym, 'Information Technology', 'USD') "
        "RETURNING id"), {"sym": symbol, "t": itype}).scalar()


def _bars(s, iid, days) -> None:
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid, :d, 100, 101, 99, 100, 1000000, 'EodhdAdapter')"),
        [{"iid": iid, "d": d} for d in days])


def _report(s, iid, fpe: date, rd: date, surprise: float,
            baf: str = "BeforeMarket") -> None:
    """One completed report with surprise = eps_actual - eps_estimate (estimate
    pinned at 0 so the surprise IS the actual — keeps the SUE math by hand)."""
    s.execute(text(
        "INSERT INTO market.earnings_surprises (instrument_id, fiscal_period_end, "
        " report_date, eps_actual, eps_estimate, surprise_pct, currency, "
        " before_after_market, source, fetched_at) "
        "VALUES (:iid, :fpe, :rd, :a, 0, NULL, NULL, :baf, 'test', :fa)"),
        {"iid": iid, "fpe": fpe, "rd": rd, "a": surprise, "baf": baf, "fa": T15})


def _fresh_series(s, iid, current_surprise: float) -> None:
    for fpe, rd, sp in _PRIORS:
        _report(s, iid, fpe, rd, sp)
    _report(s, iid, _CURRENT_FPE, _CURRENT_RD, current_surprise)


@pytest.fixture
def seeded(clean_audit):
    s = clean_audit
    _clean(s)
    # isolate the cross-section to exactly our names (rolls back with the txn)
    s.execute(text(
        "UPDATE market.instruments SET is_active = false "
        "WHERE market = 'US' AND instrument_type IN ('stock','adr') "
        "  AND symbol NOT LIKE 'PSG%'"))
    strategy_id = s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, code_sha, "
        " tolerance_bands, state, approved_by, approved_at) "
        "VALUES ('pead-sue-tr', 'foster_olsen_shevlin_sue', '1.0.0', '{}', "
        "        'test-sha', CAST(:b AS jsonb), 'paper', 'Principal (test)', :at) "
        "RETURNING id"),
        {"b": json.dumps(BANDS), "at": T15}).scalar()

    # four ranked names: fresh, defined, distinct SUE
    for sym, _, surprise in GOLDEN:
        iid = _instrument(s, sym)
        _bars(s, iid, _ELIG_DAYS)
        _fresh_series(s, iid, surprise)
    # PSGE: only three priors -> current SUE undefined
    e = _instrument(s, "PSGE")
    _bars(s, e, _ELIG_DAYS)
    for fpe, rd, sp in _PRIORS[1:]:
        _report(s, e, fpe, rd, sp)
    _report(s, e, _CURRENT_FPE, _CURRENT_RD, 10.0)
    # PSGF: defined SUE but its latest report (2026-01-05) is > 63 sessions old
    f = _instrument(s, "PSGF")
    _bars(s, f, _ELIG_DAYS)
    for fpe, rd, sp in _STALE_PRIORS:
        _report(s, f, fpe, rd, sp)
    _report(s, f, date(2025, 6, 30), date(2026, 1, 5), 8.0)
    # PSGG: fresh, defined SUE but NO close on the signal session (not tradable)
    g = _instrument(s, "PSGG")
    _bars(s, g, _PSGG_DAYS)
    _fresh_series(s, g, 99.0)

    yield s, strategy_id


def _signal_rows(s, strategy_id, on: date):
    return s.execute(text(
        "SELECT i.symbol, sg.rank, sg.formation_return, sg.signal_date, "
        "       sg.valid_until, sg.direction "
        "FROM quant.signals sg JOIN market.instruments i "
        "  ON i.id = sg.instrument_id "
        "WHERE sg.strategy_id = :sid AND sg.signal_date = :d "
        "ORDER BY sg.rank"), {"sid": strategy_id, "d": on}).all()


def _gen_events(s) -> list[dict]:
    return [r[0] for r in s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'quant.signals.generated' "
        "  AND actor_id = 'pead_signal_generation' ORDER BY seq")).all()]


# --------------------------------------------------- initiation + golden pin

def test_initiation_generates_the_hand_derived_winner_set(seeded):
    s, strategy_id = seeded
    rep = generate_pead_signals(s, FrozenClock(T15))
    assert rep.trigger == "initiation" and rep.inserted == 4
    assert rep.n_eligible == 4 and rep.session == SESSION

    rows = _signal_rows(s, strategy_id, SESSION)
    assert [(r.symbol, int(r.rank)) for r in rows] == [
        (sym, rank) for sym, rank, _ in GOLDEN]
    assert all(r.direction == "long" for r in rows)
    assert all(r.valid_until == MONTH_END for r in rows)
    # SUE stored verbatim in formation_return (the generic ranked-value column)
    for r in rows:
        assert float(r.formation_return) == pytest.approx(EXPECT_SUE[r.symbol])
    # the undefined / stale / untradable names never signal
    assert {"PSGE", "PSGF", "PSGG"}.isdisjoint({r.symbol for r in rows})

    events = _gen_events(s)
    assert len(events) == 1
    p = events[0]
    assert p["trigger"] == "initiation" and p["n_winners"] == 4
    assert p["family"] == "pead-sue-tr"
    assert p["session"] == "2026-07-15" and p["valid_until"] == "2026-07-31"
    assert p["top"][0]["symbol"] == "PSGA"


def test_rerun_is_idempotent_no_new_rows_no_new_event(seeded):
    s, strategy_id = seeded
    generate_pead_signals(s, FrozenClock(T15))
    rep2 = generate_pead_signals(s, FrozenClock(T15))
    assert rep2.inserted == 0 and rep2.existing == 4
    assert "already generated" in rep2.reason
    assert len(_signal_rows(s, strategy_id, SESSION)) == 4
    assert len(_gen_events(s)) == 1


# ------------------------------------------------- trigger machine (FrozenClock)

def test_mid_month_after_initiation_does_not_generate(seeded):
    s, strategy_id = seeded
    generate_pead_signals(s, FrozenClock(T15))
    rep = generate_pead_signals(
        s, FrozenClock(datetime(2026, 7, 16, 22, 0, tzinfo=UTC)))
    assert rep.trigger is None and "not a rebalance trigger" in rep.reason
    assert "next 2026-07-31" in rep.reason
    assert s.execute(text("SELECT count(*) FROM quant.signals "
                          "WHERE strategy_id = :sid"),
                     {"sid": strategy_id}).scalar() == 4


def test_month_end_session_generates_the_next_set(seeded):
    s, strategy_id = seeded
    generate_pead_signals(s, FrozenClock(T15))
    rep = generate_pead_signals(
        s, FrozenClock(datetime(2026, 7, 31, 22, 0, tzinfo=UTC)))
    assert rep.trigger == "month_end" and rep.session == MONTH_END
    rows = _signal_rows(s, strategy_id, MONTH_END)
    assert [(r.symbol, int(r.rank)) for r in rows] == [
        (sym, rank) for sym, rank, _ in GOLDEN]
    assert all(r.valid_until == AUG_END for r in rows)
    assert len(_gen_events(s)) == 2


def test_catch_up_after_expiry_generates_at_the_current_session(seeded):
    """Machine down across the month boundary: every stored signal has expired
    and the current session is NOT a month-end -> catch_up rather than leave the
    sleeve unsignalled; ranks are the CURRENT session's, never fabricated."""
    s, strategy_id = seeded
    generate_pead_signals(s, FrozenClock(T15))          # valid_until 2026-07-31
    rep = generate_pead_signals(
        s, FrozenClock(datetime(2026, 8, 4, 22, 0, tzinfo=UTC)))
    assert rep.trigger == "catch_up" and rep.session == date(2026, 8, 4)
    rows = _signal_rows(s, strategy_id, date(2026, 8, 4))
    assert [(r.symbol, int(r.rank)) for r in rows] == [
        (sym, rank) for sym, rank, _ in GOLDEN]
    assert all(r.valid_until == AUG_END for r in rows)


def test_no_strategy_row_means_idle(clean_audit):
    s = clean_audit
    _clean(s)
    rep = generate_pead_signals(s, FrozenClock(T15))
    assert rep.reason == ("pead signals idle (no paper/live pead-sue-tr "
                          "strategy)")
    assert not _gen_events(s)


# --------------------------------------------------------------- no look-ahead

def test_future_report_never_touches_the_ranking(seeded):
    """STRUCTURAL no-look-ahead: a report ANNOUNCED after the signal session
    (report_date 2026-10-15, an absurd surprise) must leave the ranking, the
    winner SUEs and the signal_date byte-identical to the golden."""
    s, strategy_id = seeded
    psga = s.execute(text(
        "SELECT id FROM market.instruments WHERE symbol = 'PSGA'")).scalar()
    _report(s, psga, date(2026, 9, 30), date(2026, 10, 15), 99999.0,
            baf="AfterMarket")

    rep = generate_pead_signals(s, FrozenClock(T15))
    assert rep.session == SESSION and rep.inserted == 4
    rows = _signal_rows(s, strategy_id, SESSION)
    assert [(r.symbol, int(r.rank)) for r in rows] == [
        (sym, rank) for sym, rank, _ in GOLDEN]
    # PSGA's stored SUE is still the golden one — the 99999 print never leaked
    psga_sue = next(float(r.formation_return) for r in rows if r.symbol == "PSGA")
    assert psga_sue == pytest.approx(EXPECT_SUE["PSGA"])


# ------------------------------------------------------- desk shortlist lane

def test_active_pead_signal_symbols_rank_order_and_proposal_exclusion(seeded):
    s, _ = seeded
    clock = FrozenClock(T15)
    generate_pead_signals(s, clock)
    assert active_pead_signal_symbols(s, clock) == ["PSGA", "PSGC", "PSGB", "PSGD"]

    # a non-expired proposal on PSGA removes it from the lane (already in front
    # of the Principal — no fresh memo needed tonight)
    psga = s.execute(text(
        "SELECT id FROM market.instruments WHERE symbol = 'PSGA'")).scalar()
    memo_id = s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, "
        "recommendation, evidence_refs, created_at) "
        "VALUES ('committee', 'PSGA', 'BUY', '[]', :ca) RETURNING id"),
        {"ca": clock.now()}).scalar()
    sid = s.execute(text("SELECT id FROM quant.signals LIMIT 1")).scalar()
    s.execute(text(
        "INSERT INTO trading.trade_proposals (instrument_id, market, action, "
        " committee_memo_id, signal_ids, entry_price, stop_loss, target_price, "
        " position_size, position_value_aud, state, expires_at, created_at) "
        "VALUES (:iid, 'US', 'buy', :memo, :sids, 100, 95, 120, 10, 1500, "
        "        'draft', :exp, :ca)"),
        {"iid": psga, "memo": memo_id, "sids": [sid],
         "exp": clock.now() + timedelta(hours=24), "ca": clock.now()})
    assert active_pead_signal_symbols(s, clock) == ["PSGC", "PSGB", "PSGD"]

    # past valid_until the lane empties: expired signals never route the desk
    late = FrozenClock(datetime(2026, 8, 4, 22, 0, tzinfo=UTC))
    assert active_pead_signal_symbols(s, late) == []


def test_scanned_desk_puts_pead_signal_names_first(seeded):
    """Through build_scanned_desk: the T7 shortlist LEADS with the PEAD signal
    lane (rank order), whatever the scanner does — order is budget policy."""
    from atlas.ops.daily import build_scanned_desk

    s, _ = seeded
    clock = FrozenClock(T15)
    generate_pead_signals(s, clock)
    seen: list[list[str]] = []

    class _Report:
        def summary(self) -> str:
            return "stub"

    def fake_desk(session, clk, symbols):
        seen.append(list(symbols))
        return _Report()

    build_scanned_desk(fake_desk, lambda session: [])(s, clock)
    assert len(seen) == 1
    got = seen[0]
    assert got[:4] == ["PSGA", "PSGC", "PSGB", "PSGD"]   # PEAD lane leads
    assert len(got) == len(set(got))                     # deduped


# ------------------------------------------------------------- SIGNALS block

def test_pead_signal_evidence_block_renders_numeric_verbatim(seeded):
    s, _ = seeded
    generate_pead_signals(s, FrozenClock(T15))
    sid = s.execute(text(
        "SELECT sg.id FROM quant.signals sg JOIN market.instruments i "
        "ON i.id = sg.instrument_id WHERE i.symbol = 'PSGA'")).scalar()

    got = extract_pead_signal_evidence(s, "PSGA", on=SESSION)
    assert got is not None
    ref, body = got
    assert ref == f"dcp:signal:pead:{sid}:2026-07-15"
    sue_str = f"{EXPECT_SUE['PSGA']:.4f}"
    assert f"standardized unexpected earnings {sue_str}" in body
    assert "rank 1 of 4" in body and "ADR-0013" in body
    # digits verbatim: standalone tokens for the grounding verifier
    assert {"1", "4", sue_str, "2026", "07", "15", "31", "0013"} \
        <= corpus_numeric_tokens(body)


def test_pead_signal_evidence_none_cases(seeded):
    s, strategy_id = seeded
    generate_pead_signals(s, FrozenClock(T15))
    # unknown / ineligible symbols: no fabricated line
    assert extract_pead_signal_evidence(s, "PSGE", on=SESSION) is None
    assert extract_pead_signal_evidence(s, "NOPE", on=SESSION) is None
    # past validity: the signal is stale evidence, not evidence
    assert extract_pead_signal_evidence(s, "PSGA", on=date(2026, 8, 3)) is None
    # before the signal existed
    assert extract_pead_signal_evidence(s, "PSGA", on=date(2026, 7, 14)) is None
    # a suspended strategy's signals stop being citable evidence
    s.execute(text("UPDATE quant.strategies SET state = 'suspended' "
                   "WHERE id = :sid"), {"sid": strategy_id})
    assert extract_pead_signal_evidence(s, "PSGA", on=SESSION) is None
