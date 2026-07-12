"""Scorecard planning core (atlas/dcp/scorecard.py): anchor/horizon session
math, vindication semantics, HOLD/shadow tracking, idempotency, dartboard base
rates and dissent grading (desk-review 2026-07 item 5) — all on the pure
functions, no database. The DB flow (inserts, audit event, API shape, the
analysis-only top-up, t9 wiring) lives in tests/integration/test_scorecard_pg.py.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from atlas.dcp.scorecard import (
    MemoRef,
    anchor_index,
    dartboard_baseline,
    dissent_right,
    plan_outcomes,
    vindicated,
)


def sessions(n: int, start: date = date(2026, 1, 5)) -> list[date]:
    """n consecutive weekday sessions from a Monday — a synthetic calendar
    with real weekend gaps, so 'sessions' visibly differ from calendar days."""
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def series(closes: list[str], start: date = date(2026, 1, 5),
           ) -> list[tuple[date, Decimal]]:
    return list(zip(sessions(len(closes), start),
                    (Decimal(c) for c in closes), strict=True))


def flat_spy(dates: list[date], close: str = "100") -> dict[date, Decimal]:
    return dict.fromkeys(dates, Decimal(close))


def memo(memo_id: str = "m1", symbol: str | None = "ZSC",
         recommendation: str | None = "BUY", shadow: bool = False,
         memo_date: date = date(2026, 1, 30)) -> MemoRef:
    return MemoRef(memo_id=memo_id, symbol=symbol,
                   recommendation=recommendation, shadow=shadow,
                   memo_date=memo_date)


# ---------------------------------------------------------------- anchor math

def test_anchor_on_a_session_day_is_that_session():
    days = sessions(5)                       # Mon 1/5 .. Fri 1/9
    assert anchor_index(days, date(2026, 1, 7)) == 2


def test_anchor_on_a_non_session_day_is_the_prior_bar():
    days = sessions(10)                      # includes Fri 1/9, Mon 1/12
    sat = date(2026, 1, 10)
    assert sat not in days
    assert days[anchor_index(days, sat)] == date(2026, 1, 9)   # type: ignore[index]


def test_memo_predating_the_whole_series_has_no_anchor():
    assert anchor_index(sessions(5), date(2026, 1, 2)) is None


# ------------------------------------------------------- horizon session math

def test_horizon_is_sessions_in_the_instruments_own_sequence_not_days():
    """Anchor at index 0; 21 stored sessions => the 20-session outcome exists
    (bar index 20, > 4 weekends later in calendar time) and 60 is immature."""
    bars = series(["100"] * 20 + ["110"])    # 21 sessions, last close 110
    spy = flat_spy([d for d, _ in bars])     # flat SPY: spy_return == 0
    rows, skips, already = plan_outcomes(
        [memo(memo_date=date(2026, 1, 5))], {"ZSC": bars}, spy, existing=set())
    assert already == 0
    assert [r.horizon_sessions for r in rows] == [20]
    r = rows[0]
    assert r.anchor_date == date(2026, 1, 5)
    assert r.fwd_date == bars[20][0]
    assert r.fwd_return == Decimal("0.100000")     # 110/100 - 1, 6dp quantum
    assert r.spy_return == Decimal("0.000000")
    assert r.excess == Decimal("0.100000")
    im = [s for s in skips if s.horizon_sessions == 60]
    assert len(im) == 1 and im[0].reason.startswith("immature")


def test_non_session_memo_date_anchors_to_prior_bar_and_shifts_the_window():
    """A Saturday memo anchors to Friday's bar; the 20-session forward bar is
    counted from THAT session, hand-pinned."""
    bars = series(["100"] * 5 + ["200"] + ["100"] * 18 + ["150"])  # 25 sessions
    dates = [d for d, _ in bars]
    fri = date(2026, 1, 9)                   # index 4
    assert dates[4] == fri
    sat = date(2026, 1, 10)
    rows, _, _ = plan_outcomes(
        [memo(memo_date=sat)], {"ZSC": bars}, flat_spy(dates), existing=set())
    r = rows[0]
    assert (r.anchor_date, r.horizon_sessions) == (fri, 20)
    assert r.fwd_date == dates[24]
    assert r.fwd_return == Decimal("0.500000")     # 150/100 - 1


def test_spy_return_uses_the_same_anchor_and_forward_dates():
    bars = series(["100"] * 20 + ["121"])
    dates = [d for d, _ in bars]
    spy = flat_spy(dates)
    spy[dates[0]] = Decimal("400")
    spy[dates[20]] = Decimal("440")               # SPY +10% over the window
    rows, _, _ = plan_outcomes(
        [memo(memo_date=dates[0])], {"ZSC": bars}, spy, existing=set())
    r = rows[0]
    assert r.fwd_return == Decimal("0.210000")
    assert r.spy_return == Decimal("0.100000")
    assert r.excess == Decimal("0.110000")


# ------------------------------------------------------------ fail-closed skips

def test_spy_missing_either_date_fails_closed():
    bars = series(["100"] * 21)
    dates = [d for d, _ in bars]
    no_anchor = {k: v for k, v in flat_spy(dates).items() if k != dates[0]}
    no_fwd = {k: v for k, v in flat_spy(dates).items() if k != dates[20]}
    for spy in (no_anchor, no_fwd):
        rows, skips, _ = plan_outcomes(
            [memo(memo_date=dates[0])], {"ZSC": bars}, spy, existing=set())
        assert rows == []
        assert any(s.reason.startswith("missing bars (SPY") for s in skips)


def test_unresolved_instrument_and_missing_anchor_skip_memo_level():
    bars = series(["100"] * 21)
    dates = [d for d, _ in bars]
    rows, skips, _ = plan_outcomes(
        [memo(memo_id="m1", symbol=None),               # no symbol at all
         memo(memo_id="m2", symbol="GHOST"),            # not in series map
         memo(memo_id="m3", memo_date=date(2026, 1, 2))],  # predates all bars
        {"ZSC": bars}, flat_spy(dates), existing=set())
    assert rows == []
    by_id = {s.memo_id: s for s in skips}
    assert by_id["m1"].reason.startswith("no instrument")
    assert by_id["m2"].reason.startswith("no instrument")
    assert by_id["m3"].reason.startswith("missing bars")
    assert all(s.horizon_sessions is None for s in skips)  # memo-level, once


# -------------------------------------------------- vindication semantics

def test_vindication_all_four_sign_combinations():
    up, down = Decimal("0.041000"), Decimal("-0.023000")
    assert vindicated("BUY", up, shadow=False) is True       # picked a beater
    assert vindicated("BUY", down, shadow=False) is False    # picked a laggard
    assert vindicated("REJECT", down, shadow=False) is True  # dodged a laggard
    assert vindicated("REJECT", up, shadow=False) is False   # dodged a beater


def test_dead_heat_vindicates_neither_direction():
    zero = Decimal("0.000000")
    assert vindicated("BUY", zero, shadow=False) is False
    assert vindicated("REJECT", zero, shadow=False) is False


def test_hold_and_shadow_are_never_rated():
    x = Decimal("0.100000")
    assert vindicated("HOLD", x, shadow=False) is None
    assert vindicated("HOLD", -x, shadow=False) is None
    assert vindicated(None, x, shadow=False) is None
    assert vindicated("BUY", x, shadow=True) is None         # non-actionable
    assert vindicated("REJECT", -x, shadow=True) is None


def test_hold_and_shadow_memos_are_still_tracked_in_rows():
    """Exclusion is from the RATES, not the record: the planner writes their
    outcome rows like any other memo's."""
    bars = series(["100"] * 21)
    dates = [d for d, _ in bars]
    rows, _, _ = plan_outcomes(
        [memo(memo_id="h1", recommendation="HOLD", memo_date=dates[0]),
         memo(memo_id="s1", recommendation="BUY", shadow=True,
              memo_date=dates[0])],
        {"ZSC": bars}, flat_spy(dates), existing=set())
    assert {(r.memo_id, r.horizon_sessions) for r in rows} == {("h1", 20),
                                                               ("s1", 20)}


# ------------------------------------------- dissent grading (desk-review item 5)

def test_dissent_right_is_the_exact_complement_for_directional_memos():
    up, down = Decimal("0.041000"), Decimal("-0.023000")
    for rec, e in (("BUY", up), ("BUY", down), ("REJECT", up), ("REJECT", down)):
        v = vindicated(rec, e, shadow=False)
        assert dissent_right(rec, e, shadow=False) is (not v)


def test_dead_heat_grades_the_dissent_right():
    """excess == 0 vindicates neither direction, so the dissent — the case
    against the call — grades right: the call failed to beat the passive
    core, and the rule is conservative against the desk by construction."""
    zero = Decimal("0.000000")
    assert dissent_right("BUY", zero, shadow=False) is True
    assert dissent_right("REJECT", zero, shadow=False) is True


def test_hold_and_shadow_dissent_stays_ungraded():
    """No gradable direction, no gradable dissent — mirrors vindicated()."""
    x = Decimal("0.100000")
    assert dissent_right("HOLD", x, shadow=False) is None
    assert dissent_right(None, x, shadow=False) is None
    assert dissent_right("BUY", x, shadow=True) is None
    assert dissent_right("REJECT", -x, shadow=True) is None


# --------------------------------------- dartboard base rates (desk-review item 5)

def excesses(*vals: str) -> list[Decimal]:
    return [Decimal(v) for v in vals]


def test_dartboard_baseline_hand_pinned():
    """10 tracked outcomes: 6 down, 3 up, 1 dead heat. REJECT's dart scores
    6/10, BUY's 3/10 — the dead heat counts for neither, exactly like the
    vindication rule (so the two baselines need not sum to one)."""
    xs = excesses("-0.010000", "-0.020000", "-0.030000", "-0.040000",
                  "-0.050000", "-0.060000", "0.010000", "0.020000",
                  "0.030000", "0.000000")
    assert dartboard_baseline("REJECT", xs) == Decimal("0.6")
    assert dartboard_baseline("BUY", xs) == Decimal("0.3")


def test_dartboard_baseline_empty_and_non_directional_is_none():
    assert dartboard_baseline("BUY", []) is None
    assert dartboard_baseline("REJECT", []) is None
    assert dartboard_baseline("HOLD", excesses("-0.010000")) is None
    assert dartboard_baseline(None, excesses("-0.010000")) is None


def test_always_reject_desk_in_a_bear_market_scores_zero_edge():
    """THE reason the dartboard exists (desk-review 2026-07 item 5): every
    tracked outcome is negative, the desk REJECTed everything, and raw
    vindication reads a perfect 10/10 — but a direction-blind dart thrown at
    the same outcomes scores the same 10/10. rate - baseline == 0: the desk
    demonstrated no skill the falling market didn't hand it."""
    xs = excesses(*(f"-0.0{i}0000" for i in range(1, 10)), "-0.100000")
    assert len(xs) == 10 and all(x < 0 for x in xs)
    wins = sum(1 for x in xs if vindicated("REJECT", x, shadow=False) is True)
    rate = Decimal(wins) / Decimal(len(xs))
    base = dartboard_baseline("REJECT", xs)
    assert rate == Decimal("1")
    assert base == Decimal("1")
    assert rate - base == Decimal("0")            # zero edge — that is the point


def test_vendor_adapter_factory_selects_by_key(monkeypatch):
    """vendor_adapter_for: single-entry vendor map when a key is configured
    (the daily adapter's map refuses analysis-only symbols), deterministic
    fixture adapter keyless — mirror of the ops/analyze construction."""
    import atlas.dcp.scorecard as sc
    from atlas.dcp.market_data.adapters.eodhd import EodhdAdapter
    from atlas.dcp.market_data.adapters.fixture import FixtureAdapter

    class _Keyless:
        eodhd_api_key = ""

    class _Keyed:
        eodhd_api_key = "k"

    monkeypatch.setattr(sc, "get_settings", lambda: _Keyless())
    assert isinstance(sc.vendor_adapter_for("ZSCT", "NASDAQ"), FixtureAdapter)
    monkeypatch.setattr(sc, "get_settings", lambda: _Keyed())
    keyed = sc.vendor_adapter_for("ZSCT", "NASDAQ")
    assert isinstance(keyed, EodhdAdapter)
    assert keyed._sym("ZSCT") == "ZSCT.US"        # the one-entry map, vendor rule


# ------------------------------------------------------------------ idempotency

def test_existing_rows_are_never_replanned():
    bars = series(["100"] * 61)              # both horizons mature
    dates = [d for d, _ in bars]
    m = memo(memo_date=dates[0])
    spy = flat_spy(dates)
    rows, _, already = plan_outcomes([m], {"ZSC": bars}, spy, existing=set())
    assert {r.horizon_sessions for r in rows} == {20, 60} and already == 0
    rows2, skips2, already2 = plan_outcomes(
        [m], {"ZSC": bars}, spy,
        existing={("m1", 20), ("m1", 60)})
    assert rows2 == [] and already2 == 2
    assert skips2 == []                      # recorded facts are not "skips"


def test_partial_maturity_fills_only_the_missing_horizon():
    bars = series(["100"] * 61)
    dates = [d for d, _ in bars]
    rows, _, already = plan_outcomes(
        [memo(memo_date=dates[0])], {"ZSC": bars}, flat_spy(dates),
        existing={("m1", 20)})               # 20s recorded on an earlier run
    assert [(r.memo_id, r.horizon_sessions) for r in rows] == [("m1", 60)]
    assert already == 1
