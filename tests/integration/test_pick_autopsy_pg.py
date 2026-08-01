"""Pick autopsy: winners-vs-losers feature comparison + pre-registered filter
hypotheses (docs/specs/source-pick-filter-hypotheses.md).

Proves the MEASURED-ONLY contract end to end:
  * win/loss counts per (source, horizon) — only graded picks count;
  * feature splits use the features frozen at recommendation time;
  * H1 cohorts split honestly: in-sample (on/before registration) vs
    out-of-sample (after), and picks missing the predicate's features land in
    `unknown` — never silently assigned to a cohort;
  * the read-only API endpoint serves the same numbers.
"""
from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from atlas.api.main import app
from atlas.dcp.research.source_picks import H1_REGISTERED, pick_autopsy
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg

IN_SAMPLE = date(2026, 7, 20)        # <= H1_REGISTERED (2026-08-01)
OOS = date(2026, 8, 5)               # strictly after -> out-of-sample


def _pick(s, *, source: str, ticker: str, rd: date, feats: dict,
          e5: float | None = None) -> None:
    s.execute(text(
        "INSERT INTO research.source_picks (source, ticker, recommendation_date, "
        " as_of_session, feature_version, features, excess_5) "
        "VALUES (:s, :t, :rd, :rd, 'v1', CAST(:f AS jsonb), :e5)"),
        {"s": source, "t": ticker, "rd": rd, "f": json.dumps(feats), "e5": e5})


def _seed(s) -> None:
    s.execute(text("DELETE FROM research.source_picks"))     # txn-local
    # winner in an uptrend (H1 out-cohort)
    _pick(s, source="src-a", ticker="WINA", rd=IN_SAMPLE, e5=0.05,
          feats={"mom_12_1": 0.30, "ret_20d": 0.04, "px_over_sma50": 0.03})
    # loser, falling knife (H1 in-cohort: ret_20d<0 AND px_over_sma50<0)
    _pick(s, source="src-a", ticker="LOSK", rd=IN_SAMPLE, e5=-0.06,
          feats={"mom_12_1": 0.05, "ret_20d": -0.06, "px_over_sma50": -0.04})
    # loser NOT in a downtrend (H1 out-cohort — losses alone don't make the cohort)
    _pick(s, source="src-a", ticker="LOSN", rd=IN_SAMPLE, e5=-0.01,
          feats={"mom_12_1": 0.20, "ret_20d": 0.02, "px_over_sma50": 0.01})
    # graded but features empty -> counted in W/L, `unknown` for H1
    _pick(s, source="src-b", ticker="MYST", rd=IN_SAMPLE, e5=0.02, feats={})
    # ungraded -> excluded from every block (no outcome yet)
    _pick(s, source="src-a", ticker="IMMA", rd=IN_SAMPLE,
          feats={"ret_20d": -0.05, "px_over_sma50": -0.05})
    # out-of-sample falling knife, graded a loser
    _pick(s, source="src-a", ticker="OOSK", rd=OOS, e5=-0.03,
          feats={"ret_20d": -0.03, "px_over_sma50": -0.02})


def test_win_loss_counts_graded_only(pg_session):
    _seed(pg_session)
    out = pick_autopsy(pg_session)
    wl = {(r["source"], r["horizon"]): (r["wins"], r["losses"])
          for r in out["win_loss"]}
    assert wl[("src-a", 5)] == (1, 3)          # WINA vs LOSK/LOSN/OOSK; IMMA excluded
    assert wl[("src-b", 5)] == (1, 0)
    # no pick has excess_20 -> the 20/60 horizons must not appear at all
    assert {r["horizon"] for r in out["win_loss"]} == {5}


def test_feature_split_uses_pick_time_features(pg_session):
    _seed(pg_session)
    out = pick_autopsy(pg_session)
    mom = next(f for f in out["features"]
               if f["horizon"] == 5 and f["feature"] == "mom_12_1")
    assert mom["n_win"] == 1 and mom["win_mean"] == pytest.approx(0.30)
    assert mom["n_loss"] == 2                   # OOSK has no mom_12_1 -> excluded
    assert mom["loss_mean"] == pytest.approx(0.125)
    assert mom["win_mean"] > mom["loss_mean"]   # the losing profile stays legible


def test_h1_cohorts_split_in_vs_out_of_sample(pg_session):
    _seed(pg_session)
    out = pick_autopsy(pg_session)
    assert IN_SAMPLE <= H1_REGISTERED < OOS     # the seed really straddles it
    by = {(h["sample"], h["horizon"]): h for h in out["hypotheses"]}
    ins = by[("in_sample", 5)]
    assert ins["in_cohort"]["n"] == 1           # LOSK only
    assert ins["in_cohort"]["mean_excess"] == pytest.approx(-0.06)
    assert ins["out_cohort"]["n"] == 2          # WINA + LOSN (a loss, but no knife)
    assert ins["out_cohort"]["mean_excess"] == pytest.approx(0.02)
    assert ins["unknown"] == 1                  # MYST: predicate unknowable
    oos = by[("out_of_sample", 5)]
    assert oos["in_cohort"]["n"] == 1           # OOSK
    assert oos["in_cohort"]["mean_excess"] == pytest.approx(-0.03)
    assert oos["out_cohort"]["n"] == 0 and oos["unknown"] == 0


@pytest.fixture
def client(monkeypatch, clean_audit):
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    clean_audit.execute(text("TRUNCATE research.source_picks"))
    clean_audit.commit()
    yield TestClient(app), clean_audit
    clean_audit.execute(text("TRUNCATE research.source_picks"))
    clean_audit.commit()
    reset_app_engine()


def test_autopsy_endpoint_serves_the_same_numbers(client):
    c, s = client
    _seed(s)
    s.commit()                                   # the API reads its own session
    body = c.get("/v1/research/source-picks/autopsy").json()
    wl = {(r["source"], r["horizon"]): (r["wins"], r["losses"])
          for r in body["win_loss"]}
    assert wl[("src-a", 5)] == (1, 3) and wl[("src-b", 5)] == (1, 0)
    assert any(h["sample"] == "out_of_sample" for h in body["hypotheses"])
    # the measured-only contract is stated on the wire, not just in docs
    assert "Tier-1" in body["note"]
