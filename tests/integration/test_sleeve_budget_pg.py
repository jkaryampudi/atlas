"""Sleeve-budget sizing in the memo->proposal bridge (ADR-0014, option B).

The signed active satellite is momentum 10% + PEAD 10% of NAV. Left to the
risk engine alone, each BUY memo sizes to ~1% risk / the L1 8% single-name cap
INDEPENDENTLY, so ~10 momentum names would each take 8% and aggregate to ~80%
of NAV — far past the 10% sleeve. The bridge caps each name so the family's
AGGREGATE new exposure stays inside its envelope, equal-weight across the
sleeve's BUY names; the risk engine still validates every (capped) proposal and
may shrink it further.

Empty A$100k book -> NAV = SEED_CASH = 100000 AUD; FX USD->AUD pinned at 1.0 so
price_aud == entry and every figure is exact. Calm 21-session OHLC (h=e+1,
l=e-1, c=e) -> ATR(14) = 2, stop = e-4 (the ATR stop, not the 10% floor), risk
size bound by L1 (8% single-name) unless noted. Seeding mirrors
test_bridge_signals_pg.py.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.risk.seed_limits import seed_limit_set
from atlas.dcp.trading.bridge import SLEEVE_BUDGET_FRACTION, bridge_memos
from tests.conftest import requires_pg

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
T0 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
FX = Decimal("1.0")
NAV = Decimal("100000")                       # empty A$100k book
SLEEVE_AUD = NAV * Decimal("0.10")            # 10000 AUD per sleeve (ADR-0014)
BARS_REF = "dcp:bars:ZSLV:2026-07-13"         # a NON-signal ref (uuid5 fallback)
SIG_DATE = "2026-07-13"

FAMILY_PREFIX = {"xsmom-pit-tr": "xsmom", "pead-sue-tr": "pead"}


def _clean(s) -> None:
    s.execute(text("UPDATE trading.trade_proposals "
                   "SET risk_check_id = NULL, state = 'draft'"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks", "trading.trade_proposals",
              "trading.positions", "trading.portfolio_snapshots"):
        s.execute(text(f"DELETE FROM {t}"))
    s.execute(text("DELETE FROM risk.limit_sets WHERE version > 1"))
    s.execute(text("DELETE FROM quant.sleeve_daily"))
    s.execute(text("DELETE FROM quant.signals"))
    s.execute(text("DELETE FROM quant.strategies "
                   "WHERE family IN ('xsmom-pit-tr', 'pead-sue-tr')"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZSLV%')"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol LIKE 'ZSLV%'"))


def _seed(s):
    _clean(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD', 'AUD', '2026-07-10', :r, 'test') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = :r"),
        {"r": FX})


def _strategy(s, family: str):
    return s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, code_sha, "
        " tolerance_bands, state) "
        "VALUES (:fam, :fam, '1.0.0', '{}', 'test-sha', '{}', 'paper') "
        "RETURNING id"), {"fam": family}).scalar()


def _instrument(s, symbol: str):
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, sector_gics, currency) "
        "VALUES (:sym, 'XTEST', 'US', 'stock', :sym, 'Information Technology', "
        "'USD') RETURNING id"), {"sym": symbol}).scalar()


def _ohlc(s, iid, entry: int, *, volume: int = 1_000_000,
          start: date = date(2026, 6, 23)) -> None:
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high, "
        "low, close, volume, source) "
        "VALUES (:iid, :d, :c, :h, :l, :c, :v, 'EodhdAdapter')"),
        [{"iid": iid, "d": start + timedelta(days=i), "c": entry,
          "h": entry + 1, "l": entry - 1, "v": volume} for i in range(21)])


def _signal(s, strategy_id, iid) -> str:
    return str(s.execute(text(
        "INSERT INTO quant.signals (strategy_id, instrument_id, signal_date, "
        " direction, rank, formation_return, valid_until, created_at) "
        "VALUES (:sid, :iid, :d, 'long', 1, 0.5, '2026-07-31', :ca) RETURNING id"),
        {"sid": strategy_id, "iid": iid, "d": SIG_DATE, "ca": T0}).scalar())


def _memo(s, clock, symbol: str, refs: list[str]) -> str:
    return str(s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, "
        "recommendation, evidence_refs, created_at) "
        "VALUES ('committee', :sym, 'BUY', CAST(:er AS jsonb), :ca) RETURNING id"),
        {"sym": symbol, "er": json.dumps(refs), "ca": clock.now()}).scalar())


def _sleeve_name(s, clock, strategy_id, family: str, symbol: str,
                 entry: int, *, volume: int = 1_000_000) -> None:
    """A BUY memo grounded on this family's REAL signal ref -> bridges into the
    family's sleeve."""
    iid = _instrument(s, symbol)
    _ohlc(s, iid, entry, volume=volume)
    sig = _signal(s, strategy_id, iid)
    ref = f"dcp:signal:{FAMILY_PREFIX[family]}:{sig}:{SIG_DATE}"
    _memo(s, clock, symbol, [ref, BARS_REF])


def _proposals(s):
    return {r.symbol: r for r in s.execute(text(
        "SELECT tp.position_size, tp.position_value_aud, tp.state, i.symbol "
        "FROM trading.trade_proposals tp "
        "JOIN market.instruments i ON i.id = tp.instrument_id "
        "ORDER BY i.symbol")).all()}


# --------------------------------------------------------------------------
# A — the headline: a 10-name momentum sleeve caps aggregate at 10% NAV (not
#     ~80%), while a non-sleeve memo is still sized by risk alone.
# --------------------------------------------------------------------------

def test_ten_name_sleeve_caps_aggregate_at_ten_percent_not_risk_sum(clean_audit):
    s = clean_audit
    _seed(s)
    mom = _strategy(s, "xsmom-pit-tr")
    for i in range(10):                       # 10 momentum names, entry 100
        _sleeve_name(s, FrozenClock(T0), mom, "xsmom-pit-tr", f"ZSLVM{i}", 100)
    # a NON-sleeve control (no signal ref): risk sizes it to the L1 8% cap
    ctrl = _instrument(s, "ZSLVCTRL")
    _ohlc(s, ctrl, 100)
    _memo(s, FrozenClock(T0), "ZSLVCTRL", [BARS_REF])

    report = bridge_memos(s, FrozenClock(T0))
    assert len(report.built) == 11 and all(b.verdict == "PASS" for b in report.built)
    props = _proposals(s)

    # per name: floor((10000 - 0) / 10 / 100) = 10 shares (1% NAV each), so the
    # sleeve binds far below the L1 risk size of 80 shares
    sleeve = [props[f"ZSLVM{i}"] for i in range(10)]
    assert all(int(p.position_size) == 10 for p in sleeve)
    aggregate = sum(Decimal(p.position_value_aud) for p in sleeve)
    assert aggregate == SLEEVE_AUD                        # exactly 10% of NAV
    # the control proves the UNcapped size: L1 8% * 100000 / 100 = 80 shares —
    # ten of THOSE would be ~80% of NAV, which the sleeve budget prevents
    assert int(props["ZSLVCTRL"].position_size) == 80


# --------------------------------------------------------------------------
# B — a partly-committed sleeve sizes only the REMAINING budget.
# --------------------------------------------------------------------------

def test_partly_committed_sleeve_sizes_only_the_remaining_budget(clean_audit):
    s = clean_audit
    _seed(s)
    mom = _strategy(s, "xsmom-pit-tr")
    # a live pending proposal already reserves 4000 AUD of the sleeve (seeded
    # the way the lifecycle builds one: risk_review -> PASS check -> awaiting
    # approval, so the pending_approval_requires_check constraint is satisfied)
    held_iid = _instrument(s, "ZSLVHELD")
    _ohlc(s, held_iid, 100)
    held_sig = _signal(s, mom, held_iid)
    # a STALE memo (>48h old): the proposal must reference one (agent origin),
    # but a stale thesis is not itself a bridge candidate, so it never inflates
    # tonight's sleeve name count
    held_memo = s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, "
        "recommendation, evidence_refs, created_at) "
        "VALUES ('committee', 'ZSLVHELD', 'BUY', '[]', :ca) RETURNING id"),
        {"ca": T0 - timedelta(hours=72)}).scalar()
    held_pid = s.execute(text(
        "INSERT INTO trading.trade_proposals (instrument_id, market, action, "
        " committee_memo_id, signal_ids, entry_price, stop_loss, target_price, "
        " position_size, position_value_aud, state, expires_at, created_at) "
        "VALUES (:iid, 'US', 'buy', :memo, :sids, 100, 96, 108, 40, 4000, "
        "        'risk_review', :exp, :ca) RETURNING id"),
        {"iid": held_iid, "memo": held_memo, "sids": [uuid.UUID(held_sig)],
         "exp": T0 + timedelta(hours=24), "ca": T0}).scalar()
    cid = s.execute(text(
        "INSERT INTO risk.risk_checks (proposal_id, results, verdict, check_kind) "
        "VALUES (:p, '[]', 'PASS', 'proposal') RETURNING id"),
        {"p": held_pid}).scalar()
    s.execute(text(
        "UPDATE trading.trade_proposals SET state = 'pending_approval', "
        "risk_check_id = :c WHERE id = :p"), {"c": cid, "p": held_pid})
    # three NEW momentum names split the remaining 6000 AUD
    for i in range(3):
        _sleeve_name(s, FrozenClock(T0), mom, "xsmom-pit-tr", f"ZSLVN{i}", 100)

    report = bridge_memos(s, FrozenClock(T0))
    built = {b.symbol: b for b in report.built}
    assert set(built) == {"ZSLVN0", "ZSLVN1", "ZSLVN2"}
    props = _proposals(s)
    # per name: floor((10000 - 4000) / 3 / 100) = 20 shares (2000 AUD)
    new = [props[f"ZSLVN{i}"] for i in range(3)]
    assert all(int(p.position_size) == 20 for p in new)
    new_aggregate = sum(Decimal(p.position_value_aud) for p in new)
    assert new_aggregate == Decimal("6000")              # only the remainder
    # committed + new = the full 10% envelope, never more
    assert Decimal("4000") + new_aggregate == SLEEVE_AUD


# --------------------------------------------------------------------------
# C — the risk engine can still shrink a name BELOW the sleeve envelope.
# --------------------------------------------------------------------------

def test_risk_engine_can_shrink_below_the_sleeve_envelope(clean_audit):
    s = clean_audit
    _seed(s)
    mom = _strategy(s, "xsmom-pit-tr")
    # one name: sleeve slice = floor(10000 / 1 / 100) = 100 shares, but L1's 8%
    # single-name cap = 80 shares binds first — risk shrinks below the envelope
    _sleeve_name(s, FrozenClock(T0), mom, "xsmom-pit-tr", "ZSLVSOLO", 100)

    report = bridge_memos(s, FrozenClock(T0))
    assert len(report.built) == 1 and report.built[0].verdict == "PASS"
    solo = _proposals(s)["ZSLVSOLO"]
    assert int(solo.position_size) == 80                 # L1 (80) < sleeve (100)
    assert Decimal(solo.position_value_aud) == Decimal("8000")  # 8% NAV < 10%


# --------------------------------------------------------------------------
# D — whole-share flooring leaves a residual (never over-allocates).
# --------------------------------------------------------------------------

def test_whole_share_flooring(clean_audit):
    s = clean_audit
    _seed(s)
    mom = _strategy(s, "xsmom-pit-tr")
    for i in range(3):                        # 10000 / 3 / 100 = 33.33 -> 33
        _sleeve_name(s, FrozenClock(T0), mom, "xsmom-pit-tr", f"ZSLVF{i}", 100)

    report = bridge_memos(s, FrozenClock(T0))
    assert len(report.built) == 3
    props = _proposals(s)
    per_name = [props[f"ZSLVF{i}"] for i in range(3)]
    assert all(int(p.position_size) == 33 for p in per_name)   # floored, not 34
    aggregate = sum(Decimal(p.position_value_aud) for p in per_name)
    assert aggregate == Decimal("9900")                  # 33*100*3, a residual
    assert aggregate < SLEEVE_AUD                        # never over the envelope


# --------------------------------------------------------------------------
# E — per-strategy attribution: each family capped by ITS OWN budget.
# --------------------------------------------------------------------------

def test_per_strategy_attribution_caps_each_sleeve_independently(clean_audit):
    s = clean_audit
    _seed(s)
    mom = _strategy(s, "xsmom-pit-tr")
    pead = _strategy(s, "pead-sue-tr")
    # two momentum names at entry 100, two PEAD names at entry 200
    for i in range(2):
        _sleeve_name(s, FrozenClock(T0), mom, "xsmom-pit-tr", f"ZSLVM{i}", 100)
    for i in range(2):
        _sleeve_name(s, FrozenClock(T0), pead, "pead-sue-tr", f"ZSLVP{i}", 200)

    report = bridge_memos(s, FrozenClock(T0))
    assert len(report.built) == 4 and all(b.verdict == "PASS" for b in report.built)
    props = _proposals(s)

    # momentum budget 10000 / 2 names / 100 = 50 shares each -> aggregate 10000
    mom_names = [props[f"ZSLVM{i}"] for i in range(2)]
    assert all(int(p.position_size) == 50 for p in mom_names)
    assert sum(Decimal(p.position_value_aud) for p in mom_names) == SLEEVE_AUD
    # PEAD budget 10000 / 2 names / 200 = 25 shares each -> aggregate 10000
    pead_names = [props[f"ZSLVP{i}"] for i in range(2)]
    assert all(int(p.position_size) == 25 for p in pead_names)
    assert sum(Decimal(p.position_value_aud) for p in pead_names) == SLEEVE_AUD
    # the two sleeves are separate envelopes: 10% + 10% = the signed 20% satellite
    assert SLEEVE_BUDGET_FRACTION["xsmom-pit-tr"] == Decimal("0.10")
    assert SLEEVE_BUDGET_FRACTION["pead-sue-tr"] == Decimal("0.10")
