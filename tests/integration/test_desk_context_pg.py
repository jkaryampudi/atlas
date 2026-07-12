"""Desk-context extractors (desk-review memo 2026-07 item 10): the SPY regime
label (10a) and the scanner attention-context block (10b). Hand-pinned
renders; None whenever the record is absent, stale, or malformed — never a
warmup artifact, never partial text."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from atlas.agents.runtime.grounding import corpus_numeric_tokens
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.desk_context import (extract_regime_evidence,
                                                extract_scanner_context)
from atlas.dcp.market_data.ingest import seed_instruments
from tests.conftest import requires_pg

pytestmark = requires_pg
SEEDS = Path(__file__).parents[2] / "seeds" / "instruments_seed.csv"
ON = date(2026, 7, 10)
SCAN_CLOCK = FrozenClock(datetime(2026, 7, 13, 2, 0, tzinfo=UTC))

GOLDEN_SCANNER = ("Scanner context for AVGO (deterministic scanner, criteria "
                  "version 1.0 — attention, not prediction; scan of session "
                  "2026-07-10): shortlisted with attention score 1.83, from "
                  "20-session absolute return 0.142 (cross-sectional rank 0.95) "
                  "and volume surge 1.51 (rank 0.88). Scanned 112 instruments, "
                  "108 eligible.")
GOLDEN_HELD = ("Scanner context for SPY (deterministic scanner, criteria "
               "version 1.0 — attention, not prediction; scan of session "
               "2026-07-10): on the shortlist as a held/book name (no attention "
               "score today). Scanned 112 instruments, 108 eligible.")


@pytest.fixture
def seeded(pg_session):
    seed_instruments(pg_session, SEEDS)
    pg_session.execute(text(
        "DELETE FROM market.price_bars_daily WHERE instrument_id = "
        "(SELECT id FROM market.instruments WHERE symbol = 'SPY')"))
    yield pg_session


def _spy_bars(s, n: int, step: float = 0.5, base: float = 400.0) -> None:
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high, "
        "low, close, volume, source) "
        "SELECT i.id, :d, :px, :px, :px, :px, 1000, 'EodhdAdapter' "
        "FROM market.instruments i WHERE i.symbol = 'SPY'"),
        [{"d": ON - timedelta(days=n - 1 - i), "px": base + i * step}
         for i in range(n)])


# ---------------------------------------------------------------- regime 10a

def test_regime_bull_label_hand_pinned(seeded):
    s = seeded
    _spy_bars(s, 150)
    got = extract_regime_evidence(s, on=ON)
    assert got == ("dcp:regime:v1:SPY:2026-07-10",
                   "Market regime (deterministic classifier v1, SPY benchmark): "
                   "bull as of 2026-07-10.")


def test_regime_bear_label_on_downtrend(seeded):
    s = seeded
    _spy_bars(s, 150, step=-0.5, base=600.0)
    got = extract_regime_evidence(s, on=ON)
    assert got is not None
    assert "bear as of 2026-07-10." in got[1]


def test_regime_warmup_history_returns_none_not_neutral(seeded):
    s = seeded
    _spy_bars(s, 100)   # one bar short of the first post-warmup label
    assert extract_regime_evidence(s, on=ON) is None


def test_regime_no_spy_history_returns_none(seeded):
    assert extract_regime_evidence(seeded, on=ON) is None


def test_regime_as_of_is_the_last_spy_bar_at_or_before_on(seeded):
    s = seeded
    _spy_bars(s, 150)
    got = extract_regime_evidence(s, on=ON + timedelta(days=2))  # SPY lags `on`
    assert got is not None
    assert got[0] == "dcp:regime:v1:SPY:2026-07-10"
    assert "as of 2026-07-10." in got[1]


# --------------------------------------------------------------- scanner 10b

def _scan_event(s, shortlist: list[dict], sessions: dict[str, str] | None = None,
                **overrides) -> None:
    payload = {"criteria_version": "1.0", "top_n": 5,
               "sessions": sessions or {"US": ON.isoformat(), "AU": ON.isoformat()},
               "scanned": 112, "eligible": 108, "ineligible": 4,
               "shortlist": shortlist}
    payload.update(overrides)
    PostgresAuditLog(s, SCAN_CLOCK).append(
        event_type="scanner.completed", entity_type="scanner",
        entity_id="2026-07-13", actor_type="dcp", actor_id="scanner_v1",
        payload=payload)


_AVGO_ENTRY = {"symbol": "AVGO", "held": False, "score": 1.83, "ret20_abs": 0.142,
               "ret20_rank": 0.95, "volume_surge": 1.51, "surge_rank": 0.88}


def test_scanner_context_hand_pinned_and_grounding_compatible(seeded):
    s = seeded
    _scan_event(s, [_AVGO_ENTRY])
    got = extract_scanner_context(s, "AVGO", on=ON)
    assert got == ("dcp:scanner:1.0:AVGO:2026-07-10", GOLDEN_SCANNER)
    # digits verbatim: every component is a standalone token for the verifier
    tokens = corpus_numeric_tokens(got[1])
    assert {"1.83", "0.142", "0.95", "1.51", "0.88", "112", "108",
            "20", "1.0"} <= tokens


def test_scanner_context_held_name_without_components(seeded):
    s = seeded
    _scan_event(s, [{"symbol": "SPY", "held": True, "score": None,
                     "ret20_abs": None, "ret20_rank": None,
                     "volume_surge": None, "surge_rank": None}])
    got = extract_scanner_context(s, "SPY", on=ON)
    assert got == ("dcp:scanner:1.0:SPY:2026-07-10", GOLDEN_HELD)


def test_scanner_context_held_name_with_components_renders_both(seeded):
    s = seeded
    _scan_event(s, [dict(_AVGO_ENTRY, held=True)])
    got = extract_scanner_context(s, "AVGO", on=ON)
    assert got is not None
    assert "held/book name, with attention score 1.83" in got[1]


def test_scanner_context_none_when_symbol_not_shortlisted(seeded):
    s = seeded
    _scan_event(s, [_AVGO_ENTRY])
    assert extract_scanner_context(s, "MSFT", on=ON) is None


def test_scanner_context_none_on_session_mismatch(seeded):
    s = seeded
    _scan_event(s, [_AVGO_ENTRY])   # scan covers 2026-07-10
    # an analyze-box run on another evidence date must not inherit the rank
    assert extract_scanner_context(s, "AVGO", on=date(2026, 7, 9)) is None


def test_scanner_context_none_without_any_event(seeded):
    assert extract_scanner_context(seeded, "AVGO", on=ON) is None
    assert extract_scanner_context(seeded, "NOPE-99", on=ON) is None


def test_scanner_context_latest_matching_event_wins(seeded):
    s = seeded
    _scan_event(s, [dict(_AVGO_ENTRY, score=1.5)])
    _scan_event(s, [_AVGO_ENTRY])   # appended later: higher seq
    got = extract_scanner_context(s, "AVGO", on=ON)
    assert got is not None and "attention score 1.83" in got[1]


def test_scanner_context_malformed_payload_fails_closed(seeded):
    s = seeded
    # scored entry with a missing component: fail closed, never partial text
    _scan_event(s, [{"symbol": "AVGO", "held": False, "score": 1.83}])
    assert extract_scanner_context(s, "AVGO", on=ON) is None
    # criteria_version that is not a plain version literal: fail closed
    _scan_event(s, [_AVGO_ENTRY], criteria_version="1.0; DROP EVERYTHING")
    assert extract_scanner_context(s, "AVGO", on=ON) is None
    # non-integer scanned count: fail closed
    _scan_event(s, [_AVGO_ENTRY], scanned="many")
    assert extract_scanner_context(s, "AVGO", on=ON) is None


def test_scanner_context_score_render_has_no_float_noise(seeded):
    s = seeded
    # payload floats render as plain decimal literals (no trailing zeros, no
    # scientific notation) so memo quotes ground verbatim
    _scan_event(s, [dict(_AVGO_ENTRY, score=2.0, ret20_abs=0.000001)])
    got = extract_scanner_context(s, "AVGO", on=ON)
    assert got is not None
    assert "attention score 2.0," in got[1]   # vendor-exact, never sci-notation
    assert "return 0.000001 " in got[1]       # 1e-06 would break grounding
