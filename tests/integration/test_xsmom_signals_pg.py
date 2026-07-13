"""quant.signals generation (atlas/dcp/signals/xsmom/generate.py, ADR-0010).

Hand-derived golden formation returns on a seeded panel, the
initiation-vs-month-end trigger under FrozenClock, idempotent re-runs, the
structural no-look-ahead guarantee (future bars and future splits never touch
the ranking), the desk's signal-first shortlist lane, and the SIGNALS
evidence block (numeric-verbatim under the grounding tokenizer).

Golden derivation (probe dates: w0 = t-252, w21 = t-21, t = 2026-07-15; every
other close is 100):
  ZSGB: 150/100 - 1 = +0.50  -> rank 1
  ZSGA: 100/80  - 1 = +0.25  -> rank 2
  ZSGF: raw 400 at w0, 4:1 split between w0 and w21 -> adjusted 100;
        100/100 - 1 =  0.00  -> rank 3 (split-adjust-on-read, no phantom move)
  ZSGC: 150/200 - 1 = -0.25  -> rank 4
  ZSGD: 60 stored sessions   -> ineligible (fail-closed contiguity)
  ZSGE: instrument_type etf, formation would be +8.00 -> excluded by universe
n_eligible = 4 -> winner set max(10, 0) = 10 -> all four, never padded.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from atlas.agents.runtime.grounding import corpus_numeric_tokens
from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.signals.xsmom.generate import (
    active_signal_symbols,
    extract_signal_evidence,
    generate_signals,
)
from tests.conftest import requires_pg

pytestmark = requires_pg

T15 = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)      # after the 7-15 US close
SESSION = date(2026, 7, 15)
MONTH_END = date(2026, 7, 31)
BANDS = {"max_drawdown_from_sleeve_peak": -0.40,
         "trailing_126_session_excess_vs_spy_tr_pp": -25.0,
         "demote_to": "suspended", "provisional": True}
GOLDEN = [("ZSGB", 1, 0.5), ("ZSGA", 2, 0.25), ("ZSGF", 3, 0.0),
          ("ZSGC", 4, -0.25)]


def _clean(s) -> None:
    """Committed debris only — everything this suite writes rolls back."""
    s.execute(text("DELETE FROM quant.sleeve_daily"))
    s.execute(text("DELETE FROM quant.signals"))
    s.execute(text("DELETE FROM quant.strategies WHERE family = 'xsmom-pit-tr'"))
    s.execute(text("DELETE FROM market.corporate_actions WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZSG%')"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZSG%')"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol LIKE 'ZSG%'"))


def _instrument(s, symbol: str, itype: str = "stock"):
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, sector_gics, currency) "
        "VALUES (:sym, 'XTEST', 'US', :t, :sym, 'Information Technology', 'USD') "
        "RETURNING id"), {"sym": symbol, "t": itype}).scalar()


def _bars(s, iid, closes: dict[date, float], default_days: list[date],
          default: float = 100.0) -> None:
    rows = [{"iid": iid, "d": d, "px": closes.get(d, default)}
            for d in default_days]
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid, :d, :px, :px, :px, :px, 1000000, 'EodhdAdapter')"), rows)


@pytest.fixture
def seeded(clean_audit):
    """Strategy row + the golden panel (module docstring), with every other
    US single name deactivated so the cross-section is exactly ours (the
    deactivation rolls back with the transaction)."""
    s = clean_audit
    _clean(s)
    s.execute(text(
        "UPDATE market.instruments SET is_active = false "
        "WHERE market = 'US' AND instrument_type IN ('stock','adr') "
        "  AND symbol NOT LIKE 'ZSG%'"))
    strategy_id = s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, code_sha, "
        " tolerance_bands, state, approved_by, approved_at) "
        "VALUES ('xsmom-pit-tr', 'xsmom_pit', '1.0.0', '{}', 'test-sha', "
        "        CAST(:b AS jsonb), 'paper', 'Principal (test)', :at) "
        "RETURNING id"), {"b": json.dumps(BANDS), "at": T15}).scalar()

    all_days = trading_days_between("US", date(2025, 6, 1), MONTH_END)
    window = [d for d in all_days if d <= SESSION][-253:]
    assert window[-1] == SESSION and len(window) == 253
    days = [d for d in all_days if window[0] <= d <= MONTH_END]
    w0, w21 = window[0], window[231]        # t-252, t-21

    _bars(s, _instrument(s, "ZSGA"), {w0: 80.0}, days)
    _bars(s, _instrument(s, "ZSGB"), {w21: 150.0}, days)
    _bars(s, _instrument(s, "ZSGC"), {w0: 200.0, w21: 150.0}, days)
    fid = _instrument(s, "ZSGF")
    _bars(s, fid, {w0: 400.0}, days)
    s.execute(text(
        "INSERT INTO market.corporate_actions (instrument_id, action_type, "
        "action_date, ratio) VALUES (:iid, 'split', :d, 4)"),
        {"iid": fid, "d": window[100]})
    _bars(s, _instrument(s, "ZSGE", "etf"), {w21: 900.0}, days)
    _bars(s, _instrument(s, "ZSGD"), {}, days[-60:])   # thin: 60 sessions only

    yield s, strategy_id, window


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
        "WHERE event_type = 'quant.signals.generated' ORDER BY seq")).all()]


# --------------------------------------------------- initiation + golden pin

def test_initiation_generates_the_hand_derived_winner_set(seeded):
    s, strategy_id, _ = seeded
    rep = generate_signals(s, FrozenClock(T15))
    assert rep.trigger == "initiation" and rep.inserted == 4
    assert rep.n_eligible == 4

    rows = _signal_rows(s, strategy_id, SESSION)
    assert [(r.symbol, int(r.rank), float(r.formation_return))
            for r in rows] == GOLDEN
    assert all(r.direction == "long" for r in rows)
    assert all(r.valid_until == MONTH_END for r in rows)
    # the etf and the thin series never signal
    assert {"ZSGE", "ZSGD"}.isdisjoint({r.symbol for r in rows})

    events = _gen_events(s)
    assert len(events) == 1
    p = events[0]
    assert p["trigger"] == "initiation" and p["n_winners"] == 4
    assert p["session"] == "2026-07-15" and p["valid_until"] == "2026-07-31"
    assert p["top"][0] == {"symbol": "ZSGB", "formation_return": 0.5}


def test_rerun_is_idempotent_no_new_rows_no_new_event(seeded):
    s, strategy_id, _ = seeded
    generate_signals(s, FrozenClock(T15))
    rep2 = generate_signals(s, FrozenClock(T15))
    assert rep2.inserted == 0 and rep2.existing == 4
    assert "already generated" in rep2.reason
    assert len(_signal_rows(s, strategy_id, SESSION)) == 4
    assert len(_gen_events(s)) == 1


# ------------------------------------------------- month-end trigger machine

def test_mid_month_after_initiation_does_not_generate(seeded):
    s, strategy_id, _ = seeded
    generate_signals(s, FrozenClock(T15))
    rep = generate_signals(
        s, FrozenClock(datetime(2026, 7, 16, 22, 0, tzinfo=UTC)))
    assert rep.trigger is None and "not a rebalance trigger" in rep.reason
    assert "next 2026-07-31" in rep.reason
    assert s.execute(text("SELECT count(*) FROM quant.signals "
                          "WHERE strategy_id = :sid"),
                     {"sid": strategy_id}).scalar() == 4


def test_month_end_session_generates_the_next_set(seeded):
    s, strategy_id, _ = seeded
    generate_signals(s, FrozenClock(T15))
    rep = generate_signals(
        s, FrozenClock(datetime(2026, 7, 31, 22, 0, tzinfo=UTC)))
    assert rep.trigger == "month_end" and rep.session == MONTH_END
    rows = _signal_rows(s, strategy_id, MONTH_END)
    assert len(rows) == 4                    # same eligible set, fresh ranking
    assert all(r.valid_until == date(2026, 8, 31) for r in rows)
    assert len(_gen_events(s)) == 2


def test_no_strategy_row_means_idle(clean_audit):
    s = clean_audit
    _clean(s)
    rep = generate_signals(s, FrozenClock(T15))
    assert rep.reason == "signals idle (no paper/live xsmom-pit-tr strategy)"
    assert not _gen_events(s)


# --------------------------------------------------------------- no look-ahead

def test_future_bar_and_future_split_never_touch_the_ranking(seeded):
    """STRUCTURAL no-look-ahead: a bar dated after the session (absurd price)
    and a split recorded for a later date must leave the ranking, the golden
    formations, and the signal_date byte-identical."""
    s, strategy_id, _ = seeded
    zsgc = s.execute(text(
        "SELECT id FROM market.instruments WHERE symbol = 'ZSGC'")).scalar()
    zsgb = s.execute(text(
        "SELECT id FROM market.instruments WHERE symbol = 'ZSGB'")).scalar()
    # future bar: ZSGC at an absurd 10000 the day AFTER the session (the
    # fixture seeds bars through month-end, so the 7-16 row already exists)
    updated = s.execute(text(
        "UPDATE market.price_bars_daily "
        "SET close = 10000, open = 10000, high = 10000, low = 10000 "
        "WHERE instrument_id = :iid AND bar_date = '2026-07-16'"),
        {"iid": zsgc}).rowcount
    assert updated == 1
    # future split: ZSGB 10:1 recorded for two days after the session
    s.execute(text(
        "INSERT INTO market.corporate_actions (instrument_id, action_type, "
        "action_date, ratio) VALUES (:iid, 'split', '2026-07-17', 10)"),
        {"iid": zsgb})

    rep = generate_signals(s, FrozenClock(T15))
    assert rep.session == SESSION            # never advanced by the future bar
    rows = _signal_rows(s, strategy_id, SESSION)
    assert [(r.symbol, int(r.rank), float(r.formation_return))
            for r in rows] == GOLDEN


# ------------------------------------------------------- desk shortlist lane

def test_active_signal_symbols_rank_order_and_proposal_exclusion(seeded):
    s, _, _ = seeded
    clock = FrozenClock(T15)
    generate_signals(s, clock)
    assert active_signal_symbols(s, clock) == ["ZSGB", "ZSGA", "ZSGF", "ZSGC"]

    # a non-expired proposal on ZSGB removes it from the lane (any state)
    memo_id = s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, "
        "recommendation, evidence_refs, created_at) "
        "VALUES ('committee', 'ZSGB', 'BUY', '[]', :ca) RETURNING id"),
        {"ca": clock.now()}).scalar()
    zsgb = s.execute(text(
        "SELECT id FROM market.instruments WHERE symbol = 'ZSGB'")).scalar()
    sid = s.execute(text(
        "SELECT id FROM quant.signals LIMIT 1")).scalar()
    s.execute(text(
        "INSERT INTO trading.trade_proposals (instrument_id, market, action, "
        " committee_memo_id, signal_ids, entry_price, stop_loss, target_price, "
        " position_size, position_value_aud, state, expires_at, created_at) "
        "VALUES (:iid, 'US', 'buy', :memo, :sids, 100, 95, 120, 10, 1500, "
        "        'draft', :exp, :ca)"),
        {"iid": zsgb, "memo": memo_id, "sids": [sid],
         "exp": clock.now() + timedelta(hours=24), "ca": clock.now()})
    assert active_signal_symbols(s, clock) == ["ZSGA", "ZSGF", "ZSGC"]

    # past valid_until the lane empties: expired signals never route the desk
    late = FrozenClock(datetime(2026, 8, 4, 22, 0, tzinfo=UTC))
    assert active_signal_symbols(s, late) == []


def test_scanned_desk_puts_signal_names_first(seeded):
    """End to end through build_scanned_desk: the T7 shortlist LEADS with the
    signal lane (rank order, minus already-proposed names), then the scanner's
    picks, deduped — order is budget policy (the breaker halts the tail)."""
    from atlas.ops.daily import build_scanned_desk

    s, _, _ = seeded
    clock = FrozenClock(T15)
    generate_signals(s, clock)
    seen: list[list[str]] = []

    class _Report:
        def summary(self) -> str:
            return "stub"

    def fake_desk(session, clk, symbols):
        seen.append(list(symbols))
        return _Report()

    report = build_scanned_desk(fake_desk, lambda session: [])(s, clock)
    assert not report.scan_failed
    assert len(seen) == 1
    got = seen[0]
    assert got[:4] == ["ZSGB", "ZSGA", "ZSGF", "ZSGC"]   # signal lane leads
    assert len(got) == len(set(got))                     # deduped


# ------------------------------------------------------------- SIGNALS block

def test_signal_evidence_block_renders_numeric_verbatim(seeded):
    s, _, _ = seeded
    generate_signals(s, FrozenClock(T15))
    sid = s.execute(text(
        "SELECT sg.id FROM quant.signals sg JOIN market.instruments i "
        "ON i.id = sg.instrument_id WHERE i.symbol = 'ZSGB'")).scalar()

    got = extract_signal_evidence(s, "ZSGB", on=SESSION)
    assert got is not None
    ref, body = got
    assert ref == f"dcp:signal:xsmom:{sid}:2026-07-15"
    assert body == (
        f"Quant signal for ZSGB (strategy family xsmom-pit-tr, state paper — "
        f"approved for paper trading, ADR-0010): cross-sectional 12-1 "
        f"momentum winner, rank 1 of 4, formation return 50.00 percent, "
        f"signal session 2026-07-15, valid until 2026-07-31. Signal id {sid}. "
        f"The signal is citable evidence for a BUY; sizing, pricing and "
        f"execution remain with the DCP and the risk engine.")
    # digits verbatim: standalone tokens for the grounding verifier
    assert {"1", "4", "50.00", "12", "2026", "07", "15", "31",
            "0010"} <= corpus_numeric_tokens(body)


def test_signal_evidence_none_cases(seeded):
    s, strategy_id, _ = seeded
    generate_signals(s, FrozenClock(T15))
    # unknown / unsignalled symbols: no fabricated line
    assert extract_signal_evidence(s, "ZSGD", on=SESSION) is None
    assert extract_signal_evidence(s, "NOPE", on=SESSION) is None
    # past validity: the signal is stale evidence, not evidence
    assert extract_signal_evidence(s, "ZSGB", on=date(2026, 8, 3)) is None
    # before the signal existed
    assert extract_signal_evidence(s, "ZSGB", on=date(2026, 7, 14)) is None
    # a suspended strategy's signals stop being citable evidence
    s.execute(text("UPDATE quant.strategies SET state = 'suspended' "
                   "WHERE id = :sid"), {"sid": strategy_id})
    assert extract_signal_evidence(s, "ZSGB", on=SESSION) is None
