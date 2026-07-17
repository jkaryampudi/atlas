"""One-shot band derivation tool (atlas/tools/derive_bands.py, board item 7).

The tool's REFUSAL and APPLY paths are exercised end-to-end against the
isolated DB with the curve regeneration monkeypatched to a synthetic pair
(regenerating the real 14-year panel belongs to the orchestrated run, not
the suite). The synthetic pair is the hand-derived golden-A construction
from tests/unit/test_band_derivation.py: 327 sessions, SPY flat, window
returns chosen so the 1st-percentile trailing-126s excess is exactly
-10.0 pp — STRICTER than the ADR-0010 provisional -25pp, so an apply must
tighten that band and record the whole decision verbatim on the audit chain.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

import atlas.tools.derive_bands as tool
from atlas.core.clock import FrozenClock
from atlas.dcp.trading.bands import DD_BAND_KEY, EXCESS_BAND_KEY
from tests.conftest import requires_pg

pytestmark = requires_pg

NOW = FrozenClock(datetime(2026, 7, 17, 3, 0, tzinfo=UTC))
PROVISIONAL = {"provisional": True, "demote_to": "suspended",
               "derivation": "ADR-0010 provisional",
               DD_BAND_KEY: -0.40, EXCESS_BAND_KEY: -25.0}


def _golden_a() -> tool.RegeneratedCurves:
    n = 327
    w = {t: 0.05 for t in range(126, n)}
    w[130], w[200], w[260] = -0.30, -0.20, -0.10
    c = [1.0] * 126
    for t in range(126, n):
        c.append(c[t - 126] * (1.0 + w[t]))
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(n)]
    return tool.RegeneratedCurves(dates=dates, strategy=c, spy=[1.0] * n,
                                  note="synthetic golden-A (test)")


def _clean(s) -> None:
    s.execute(text("DELETE FROM quant.sleeve_daily"))
    s.execute(text("DELETE FROM quant.signals"))
    s.execute(text("DELETE FROM quant.strategies "
                   "WHERE family IN ('xsmom-pit-tr', 'pead-sue-tr')"))


def _strategy(s, bands: dict):
    return s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, code_sha, "
        " tolerance_bands, state) "
        "VALUES ('xsmom-pit-tr', 'xsmom_pit', '1.0.0', '{}', 'test-sha', "
        "        CAST(:b AS jsonb), 'paper') RETURNING id"),
        {"b": json.dumps(bands)}).scalar()


def _stored_bands(s, sid) -> dict:
    return s.execute(text(
        "SELECT tolerance_bands FROM quant.strategies WHERE id = :i"),
        {"i": sid}).scalar_one()


def _derived_events(s) -> list[dict]:
    return [r[0] for r in s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'quant.strategy.bands_derived' ORDER BY seq")).all()]


@pytest.fixture
def golden_regen(monkeypatch):
    monkeypatch.setitem(tool.REGENERATORS, "xsmom-pit-tr",
                        lambda session: _golden_a())


# ---------------------------------------------------------------- refusals

def test_unknown_family_is_refused(clean_audit):
    s = clean_audit
    _clean(s)
    lines: list[str] = []
    assert tool.run(s, NOW, family="quality-pit", apply=True,
                    out=lines.append) == 1
    assert lines[0].startswith("REFUSED: unknown family")
    assert not _derived_events(s)


def test_missing_strategy_row_is_refused(clean_audit, golden_regen):
    s = clean_audit
    _clean(s)
    lines: list[str] = []
    assert tool.run(s, NOW, family="xsmom-pit-tr", apply=True,
                    out=lines.append) == 1
    assert "no xsmom-pit-tr row" in lines[0] and "REFUSED" in lines[0]
    assert not _derived_events(s)


def test_apply_refuses_to_loosen_and_writes_nothing(clean_audit):
    """Defense in depth: even a hand-crafted proposal that would loosen a
    stored band is refused at the write path — no update, no audit event."""
    s = clean_audit
    _clean(s)
    sid = _strategy(s, PROVISIONAL)
    looser = {**PROVISIONAL, "provisional": False, DD_BAND_KEY: -0.60}
    err = tool.apply_proposal(s, NOW, strategy_id=sid, family="xsmom-pit-tr",
                              proposed=looser)
    assert err is not None and "LOOSEN" in err and "signed ADR" in err
    assert _stored_bands(s, sid) == PROVISIONAL          # untouched
    assert not _derived_events(s)


# ------------------------------------------------------------ dry run/apply

def test_dry_run_prints_but_writes_nothing(clean_audit, golden_regen):
    s = clean_audit
    _clean(s)
    sid = _strategy(s, PROVISIONAL)
    lines: list[str] = []
    assert tool.run(s, NOW, family="xsmom-pit-tr", apply=False,
                    out=lines.append) == 0
    assert any("dry run — nothing written" in ln for ln in lines)
    assert _stored_bands(s, sid) == PROVISIONAL
    assert not _derived_events(s)


def test_apply_tightens_stores_and_audits_verbatim(clean_audit, golden_regen):
    s = clean_audit
    _clean(s)
    sid = _strategy(s, PROVISIONAL)
    lines: list[str] = []
    assert tool.run(s, NOW, family="xsmom-pit-tr", apply=True,
                    out=lines.append) == 0

    stored = _stored_bands(s, sid)
    assert stored["provisional"] is False
    # golden-A: derived excess p1 = -10.0pp, STRICTER than -25 -> tightened
    assert stored[EXCESS_BAND_KEY] == pytest.approx(-10.0, rel=1e-9)
    # golden-A full-window max DD = -1/3 (0.735 from peak 1.1025), x1.1 margin
    assert stored[DD_BAND_KEY] == pytest.approx(-1.0 / 3.0 * 1.1, rel=1e-9)
    assert stored["demote_to"] == "suspended"
    dec = stored["derivation"]["decisions"]
    assert dec[EXCESS_BAND_KEY]["tightened"] is True
    assert dec[DD_BAND_KEY]["tightened"] is True
    assert stored["derivation"]["excess_windows"] == 201
    assert stored["cusum"]["sigma_daily_excess"] > 0
    assert stored["cusum"]["k_sigma"] == 0.5

    events = _derived_events(s)
    assert len(events) == 1
    p = events[0]
    assert p["family"] == "xsmom-pit-tr"
    assert p["old"] == PROVISIONAL                       # verbatim old
    assert p["new"][EXCESS_BAND_KEY] == stored[EXCESS_BAND_KEY]  # verbatim new
    assert "enforced twice" in p["tighten_only"]
    assert any("APPLIED" in ln for ln in lines)


def test_reapply_composes_tighten_only_on_the_derived_row(clean_audit,
                                                          golden_regen):
    """A second derivation runs against the now-derived standing bands: the
    identical curve derives identical values (not stricter), so both bands
    are KEPT and the apply still succeeds (equal is not loosening)."""
    s = clean_audit
    _clean(s)
    sid = _strategy(s, PROVISIONAL)
    assert tool.run(s, NOW, family="xsmom-pit-tr", apply=True,
                    out=lambda _l: None) == 0
    first = _stored_bands(s, sid)
    assert tool.run(s, NOW, family="xsmom-pit-tr", apply=True,
                    out=lambda _l: None) == 0
    second = _stored_bands(s, sid)
    assert second[DD_BAND_KEY] == first[DD_BAND_KEY]
    assert second[EXCESS_BAND_KEY] == first[EXCESS_BAND_KEY]
    dec = second["derivation"]["decisions"]
    assert dec[DD_BAND_KEY]["tightened"] is False        # equal -> kept
    assert len(_derived_events(s)) == 2                  # both applies audited
