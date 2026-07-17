"""CUSUM drift early-warning (bands.check_cusum, board item 7).

The detector replays the STORED quant.sleeve_daily series against the derived
contract's parameters (tolerance_bands.cusum, written by band_derivation.py).
Hand-derivation of the drift scenario: sigma = 0.01 and mu = 0, sleeve value
falling exactly 1%/session vs a flat SPY TR -> every residual is -1.0 sigma;
with k = 0.5 the negative CUSUM grows 0.5/step and crosses h = 5.0 on the
11th residual, so a 30-row series is well past breach.

THE CONTRACT UNDER TEST: a latched breach appends ONE audit event and pages
at high priority — and NEVER demotes (the strategy stays 'paper'; demotion
authority stays with the two tolerance bands, which have signed criteria).
Later runs stay quiet (audit-event existence suppresses re-paging).
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

import atlas.dcp.trading.bands as bands_mod
from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.trading.bands import DD_BAND_KEY, EXCESS_BAND_KEY, check_cusum
from tests.conftest import requires_pg

pytestmark = requires_pg

T15 = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)
T16 = datetime(2026, 7, 16, 22, 0, tzinfo=UTC)
CUSUM = {"k_sigma": 0.5, "h_sigma": 5.0, "mean_daily_excess": 0.0,
         "sigma_daily_excess": 0.01, "action_on_breach": "page-only"}
DERIVED_BANDS = {"provisional": False, "demote_to": "suspended",
                 DD_BAND_KEY: -0.40, EXCESS_BAND_KEY: -25.0, "cusum": CUSUM}
PROVISIONAL_BANDS = {"provisional": True, "demote_to": "suspended",
                     DD_BAND_KEY: -0.40, EXCESS_BAND_KEY: -25.0}


def _clean(s) -> None:
    s.execute(text("DELETE FROM quant.sleeve_daily"))
    s.execute(text("DELETE FROM quant.signals"))
    s.execute(text("DELETE FROM quant.strategies "
                   "WHERE family IN ('xsmom-pit-tr', 'pead-sue-tr')"))


def _strategy(s, bands: dict, *, state: str = "paper",
              family: str = "xsmom-pit-tr"):
    return s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, code_sha, "
        " tolerance_bands, state) "
        "VALUES (:f, 'xsmom_pit', '1.0.0', '{}', 'test-sha', "
        "        CAST(:b AS jsonb), :st) RETURNING id"),
        {"f": family, "b": json.dumps(bands), "st": state}).scalar()


def _sessions(n: int, *, until: date = date(2026, 7, 15)) -> list[date]:
    return trading_days_between("US", date(2026, 1, 2), until)[-n:]


def _forge_series(s, strategy_id, sessions: list[date], values: list[Decimal],
                  spy: list[Decimal]) -> None:
    s.execute(text(
        "INSERT INTO quant.sleeve_daily (strategy_id, session_date, "
        " sleeve_value, spy_tr_close, peak_value, drawdown, created_at) "
        "VALUES (:sid, :d, :v, :spy, :v, 0, :ca)"),
        [{"sid": strategy_id, "d": d, "v": str(v), "spy": str(sp),
          "ca": T15} for d, v, sp in zip(sessions, values, spy, strict=True)])


def _drifted(n: int) -> list[Decimal]:
    """Sleeve value falling exactly 1%/session: residual -1.0 sigma each."""
    out, v = [], Decimal("3600")
    for _ in range(n):
        out.append(v)
        v *= Decimal("0.99")
    return out


@pytest.fixture
def alerts(monkeypatch):
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        bands_mod, "notify",
        lambda title, msg, *, priority="default": sent.append(
            (title, msg, priority)) or True)
    return sent


def _cusum_events(s) -> list[dict]:
    return [r[0] for r in s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'quant.strategy.cusum_breach' ORDER BY seq")).all()]


def _state(s, strategy_id) -> str:
    return s.execute(text("SELECT state FROM quant.strategies WHERE id = :i"),
                     {"i": strategy_id}).scalar_one()


# ------------------------------------------------------------------ breach

def test_injected_drift_pages_latches_and_never_demotes(clean_audit, alerts):
    s = clean_audit
    _clean(s)
    sid = _strategy(s, DERIVED_BANDS)
    sessions = _sessions(30)
    _forge_series(s, sid, sessions, _drifted(30), [Decimal("100")] * 30)

    report = check_cusum(s, FrozenClock(T15))
    assert [st.action for st in report.statuses] == ["breach"]
    st = report.statuses[0]
    # residuals of -1 sigma with k=0.5 grow neg by 0.5/step; the detector
    # LATCHES at the crossing — the 11th residual takes neg to 5.5 > h=5.0
    # and accumulation stops there (drift.py: breached short-circuits update)
    assert st.observations == 29
    assert st.neg == pytest.approx(5.5, rel=1e-9)
    assert st.pos == 0.0

    # THE HARD ASSERTION: no demotion — the strategy is still 'paper'
    assert _state(s, sid) == "paper"

    events = _cusum_events(s)
    assert len(events) == 1
    p = events[0]
    assert p["demoted"] is False and p["latching"] is True
    assert p["family"] == "xsmom-pit-tr" and p["observations"] == 29
    assert p["k_sigma"] == 0.5 and p["h_sigma"] == 5.0
    assert "page-only" in p["action"]
    assert len(alerts) == 1 and alerts[0][2] == "high"
    assert "CUSUM drift" in alerts[0][0]
    assert "no demotion" in alerts[0][1]
    # no band demotion event either — check_cusum touches no strategy state
    assert s.execute(text(
        "SELECT count(*) FROM audit.decision_events "
        "WHERE event_type = 'quant.strategy.demoted'")).scalar() == 0


def test_breach_is_latched_across_runs_no_duplicate_page(clean_audit, alerts):
    s = clean_audit
    _clean(s)
    sid = _strategy(s, DERIVED_BANDS)
    _forge_series(s, sid, _sessions(30), _drifted(30), [Decimal("100")] * 30)
    assert [st.action for st in check_cusum(s, FrozenClock(T15)).statuses] \
        == ["breach"]

    # next session: one more stored row, the replay breaches again — but the
    # existing audit event suppresses a second page
    _forge_series(s, sid, [date(2026, 7, 16)],
                  [_drifted(31)[-1]], [Decimal("100")])
    again = check_cusum(s, FrozenClock(T16))
    assert [st.action for st in again.statuses] == ["latched"]
    assert len(_cusum_events(s)) == 1 and len(alerts) == 1
    assert _state(s, sid) == "paper"


# ------------------------------------------------------------------- quiet

def test_no_drift_is_quiet(clean_audit, alerts):
    s = clean_audit
    _clean(s)
    sid = _strategy(s, DERIVED_BANDS)
    _forge_series(s, sid, _sessions(30), [Decimal("3600")] * 30,
                  [Decimal("100")] * 30)
    report = check_cusum(s, FrozenClock(T15))
    assert [st.action for st in report.statuses] == ["ok"]
    assert report.statuses[0].pos == 0.0 and report.statuses[0].neg == 0.0
    assert not _cusum_events(s) and not alerts
    assert _state(s, sid) == "paper"


def test_provisional_bands_without_cusum_params_are_skipped(clean_audit, alerts):
    s = clean_audit
    _clean(s)
    sid = _strategy(s, PROVISIONAL_BANDS)
    _forge_series(s, sid, _sessions(30), _drifted(30), [Decimal("100")] * 30)
    report = check_cusum(s, FrozenClock(T15))
    assert [st.action for st in report.statuses] == ["no-params"]
    assert "provisional" in report.summary()
    assert not _cusum_events(s) and not alerts


def test_suspended_strategies_are_not_replayed(clean_audit, alerts):
    s = clean_audit
    _clean(s)
    sid = _strategy(s, DERIVED_BANDS, state="suspended")
    _forge_series(s, sid, _sessions(30), _drifted(30), [Decimal("100")] * 30)
    report = check_cusum(s, FrozenClock(T15))
    assert report.statuses == ()
    assert report.summary() == "cusum idle (no banded strategy)"
    assert not _cusum_events(s) and not alerts


# --------------------------------------------------------------- governance

def test_malformed_cusum_block_raises(clean_audit):
    s = clean_audit
    _clean(s)
    bad = {**DERIVED_BANDS, "cusum": {**CUSUM, "sigma_daily_excess": 0}}
    _strategy(s, bad)
    with pytest.raises(RuntimeError, match="degenerate"):
        check_cusum(s, FrozenClock(T15))


def test_missing_cusum_keys_raise(clean_audit):
    s = clean_audit
    _clean(s)
    bad = {**DERIVED_BANDS, "cusum": {"k_sigma": 0.5}}
    _strategy(s, bad)
    with pytest.raises(RuntimeError, match="malformed"):
        check_cusum(s, FrozenClock(T15))
