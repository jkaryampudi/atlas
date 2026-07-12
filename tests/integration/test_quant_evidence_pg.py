"""Registry-driven quant evidence against real Postgres (desk-review 2026-07
item 1): the SQL joins (trial_registry <- audit payload trial_id), the jsonb
round trip, the approval-state read, and the build_evidence wiring — block 3
is deterministic DCP output, never a scraped report.

Seeding is txn-local (pg_session rolls back); quant tables are cleared inside
the transaction first so committed leftovers from other suites cannot shape
the golden.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text

from atlas.agents.live_run import build_evidence
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.backtest.quant_evidence import build_quant_evidence
from tests.conftest import requires_pg

pytestmark = requires_pg

T = datetime(2026, 7, 12, 22, 0, tzinfo=UTC)


def _clear_quant(s) -> None:
    for tbl in ("quant.validation_reports", "quant.backtests",
                "quant.strategies", "quant.trial_registry"):
        s.execute(text(f"DELETE FROM {tbl}"))


def _trial(s, family: str, created: str) -> str:
    return str(s.execute(text(
        "INSERT INTO quant.trial_registry (strategy_family, spec_hash, "
        "metrics, created_at) VALUES (:f, 'h', '{}', :c) RETURNING id"),
        {"f": family, "c": created}).scalar())


def _gate_event(s, entity_id: str, payload: dict) -> None:
    PostgresAuditLog(s, FrozenClock(T)).append(
        event_type="quant.backtest.completed", entity_type="strategy",
        entity_id=entity_id, actor_type="dcp", actor_id="test_run",
        payload=payload)


def _seed_record(s) -> None:
    _clear_quant(s)
    mom = _trial(s, "momentum", "2026-07-12T09:00:00Z")
    pit = _trial(s, "xsmom-pit", "2026-07-12T10:00:00Z")
    fx = _trial(s, "fxlab-ma_cross", "2026-07-12T11:00:00Z")   # sealed: excluded
    _gate_event(s, "momentum/SPY", {
        "trial_id": mom, "gate_passed": False,
        "window": "2010-01-04..2026-07-10", "dsr": 0.18183918826328527,
        "null_p": 0.923, "wf_positive_folds": 2,
        "gate_reasons": ["null-model: p=0.923 > 0.05 (random entries do as well)",
                         "does not beat buy-and-hold (7.8% <= 544.2%)",
                         "deflated Sharpe 0.18 < 0.9 at n_trials=6"]})
    _gate_event(s, "xsmom-pit/portfolio", {
        "trial_id": pit, "gate_passed": True,
        "window": "2010-01-04..2026-07-10", "dsr": 0.9976925177512832,
        "null_p": 0.0, "wf_positive_folds": 4, "gate_reasons": [],
        "universe": "point-in-time GSPC.INDX",
        "survivorship_note": ("point-in-time membership INCLUDING delisted "
                              "names — the definitive test of the "
                              "S&P-100/ETF conditional chain")})
    _gate_event(s, "fxlab/ma_cross", {
        "trial_id": fx, "gate_passed": False, "window": "2010-01-01..2026-07-10",
        "dsr": 0.1, "null_p": 0.512, "wf_positive_folds": 1,
        "gate_reasons": ["does not beat doing nothing"]})


GOLDEN = (
    "Quant validation record for IBN (deterministic DCP render v1 from "
    "quant.trial_registry and recorded gate verdicts; record as of "
    "2026-07-12). Per-family verdicts, key numbers and dates quoted from the "
    "recorded record:\n"
    "- momentum: gate FAIL. 1 gate run(s) recorded (scopes: SPY), 0 passed; "
    "1 trial(s) registered. Headline run momentum/SPY, window "
    "2010-01-04..2026-07-10 (decision-grade, 16 years), recorded 2026-07-12: "
    "DSR 0.182, null-model p 0.923, walk-forward positive folds 2. Gate "
    "reasons verbatim: null-model: p=0.923 > 0.05 (random entries do as "
    "well); does not beat buy-and-hold (7.8% <= 544.2%); deflated Sharpe "
    "0.18 < 0.9 at n_trials=6.\n"
    "- xsmom-pit: gate PASS recorded 2026-07-12 (point-in-time GSPC.INDX) — "
    "superseded by the total-return re-score xsmom-pit-tr (both TR runs "
    "PASS, recorded 2026-07-13); NOT approved for trading. 1 gate run(s) recorded (scopes: portfolio), 1 "
    "passed; 1 trial(s) registered. Window 2010-01-04..2026-07-10 "
    "(decision-grade, 16 years): DSR 0.998, null-model p 0.000, walk-forward "
    "positive folds 4. Recorded caveat verbatim: point-in-time membership "
    "INCLUDING delisted names — the definitive test of the S&P-100/ETF "
    "conditional chain.\n"
    "Approval state: quant.strategies holds no strategy in state "
    "approved/live/paper — NO strategy is approved for trading. No recorded "
    "gate run tests IBN directly; the verdicts above are family-level. Any "
    "thesis on IBN is untested, not merely unproven.")


def test_end_to_end_golden_through_postgres(clean_audit):
    s = clean_audit
    _seed_record(s)
    ref, body = build_quant_evidence(s, "IBN")
    assert ref == "dcp:quant:verdicts:v1:IBN"
    assert body == GOLDEN                 # jsonb + joins perturbed nothing
    assert "fxlab" not in body            # the sealed lab never reaches the desk


def test_validated_state_alone_never_reads_as_approved(clean_audit):
    """The dev DB carries a synthetic P3-canary strategies row in state
    'validated' — approval is approved/live/paper ONLY."""
    s = clean_audit
    _seed_record(s)
    s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, state) "
        "VALUES ('momentum', 'trend_rs_vol', '1.0.0', 'validated')"))
    _, body = build_quant_evidence(s, "IBN")
    assert "NO strategy is approved for trading" in body
    s.execute(text("UPDATE quant.strategies SET state = 'approved' "
                   "WHERE name = 'trend_rs_vol'"))
    _, body = build_quant_evidence(s, "IBN")
    assert ("Approval state: quant.strategies holds approved/live/paper "
            "rows for: momentum.") in body


def test_empty_registry_fallback_through_postgres(clean_audit):
    s = clean_audit
    _clear_quant(s)
    _, body = build_quant_evidence(s, "ZZZ")
    assert body.startswith("Quant validation record (deterministic DCP "
                           "render v1): quant.trial_registry holds no "
                           "registered trials")
    assert "untested, not merely unproven" in body


def test_build_evidence_block_3_is_the_registry_render(clean_audit):
    """The desk wiring: block 3 of build_evidence IS the deterministic render
    (the old REPORT constant and regex scrape are gone)."""
    s = clean_audit
    _seed_record(s)
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, currency) "
        "VALUES ('ZQEV', 'XTEST', 'US', 'stock', 'Quant Evidence Corp', 'USD') "
        "RETURNING id")).scalar()
    last = date(2026, 7, 10)
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid, :d, 100, 100, 100, 100, 1000, 'EodhdAdapter')"),
        [{"iid": iid, "d": last - timedelta(days=59 - i)} for i in range(60)])
    evidence = build_evidence(s, "ZQEV")
    ref, body = evidence[2]
    assert ref == "dcp:quant:verdicts:v1:ZQEV"
    assert body == build_quant_evidence(s, "ZQEV")[1]
    assert "superseded by the total-return re-score xsmom-pit-tr" in body
