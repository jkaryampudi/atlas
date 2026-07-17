"""Feature backfill (ADR-0011 step 1): fail-soft per symbol, ONE audit event
with honest counts, exit-2-on-failure CLI contract, idempotent re-runs.

Uses the SUE feature (earnings-only inputs) and seeds NO price bars: the CLI
path COMMITS through session_scope, and committed stray bars would pollute
the equivalence suite's production-ranker scans. Instrument/earnings seeding
is idempotent (ON CONFLICT) so committed leftovers never break a re-run."""
from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from atlas.core.clock import FrozenClock
from atlas.dcp.features.backfill import backfill_feature, main, trading_universe
from atlas.dcp.features.definitions import SUE_FOS
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg

CLOCK = FrozenClock(datetime(2025, 7, 1, 8, 0, tzinfo=UTC))
FROM, TO = date(2025, 2, 24), date(2025, 2, 28)     # one US trading week


@contextmanager
def pg_scope():
    """A COMMITTING session on the test DB for CLI fixtures (the CLI reads
    through its own session_scope, so its seeds must be committed)."""
    engine = create_engine(URL)
    s = sessionmaker(bind=engine)()
    try:
        yield s
        s.commit()
    finally:
        s.close()
        engine.dispose()


def _seed_instrument(s, sym, itype="stock", active=True):
    """active=False for anything COMMITTED (the CLI path): a committed
    active US instrument with no fixture bars would flip the daily-ingest
    coverage gates RED for the whole suite. Symbol-based materialization
    does not consult is_active, so the CLI still computes it."""
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, currency, is_active) "
        "VALUES (:s, 'XBFT', 'US', :t, :s, 'USD', :a) "
        "ON CONFLICT (symbol, exchange) DO UPDATE SET is_active = EXCLUDED.is_active "
        "RETURNING id"), {"s": sym, "t": itype, "a": active}).scalar()


def _seed_reports(s, iid):
    """Ten clean quarters (estimate 1.00, varied surprises): SUE is live and
    defined through the FROM..TO week (report 8 lands BeforeMarket
    2025-02-14 with eight defined priors)."""
    surprises = ("0.10", "0.20", "-0.10", "0.05", "0.15",
                 "0.30", "-0.05", "0.10", "0.20", "0.40")
    y, m, days = 2022, 12, {3: 31, 6: 30, 9: 30, 12: 31}
    for surp in surprises:
        fpe = date(y, m, days[m])
        m += 3
        if m > 12:
            m, y = m - 12, y + 1
        s.execute(text(
            "INSERT INTO market.earnings_surprises (instrument_id, "
            "fiscal_period_end, report_date, eps_actual, eps_estimate, "
            "surprise_pct, currency, before_after_market, source, fetched_at) "
            "VALUES (:iid, :fpe, :rd, :a, '1.00', NULL, 'USD', 'BeforeMarket', "
            "'test', :fa) "
            "ON CONFLICT (instrument_id, fiscal_period_end) DO NOTHING"),
            {"iid": iid, "fpe": fpe, "rd": fpe + timedelta(days=45),
             "a": str(Decimal("1.00") + Decimal(surp)), "fa": CLOCK.now()})


def test_backfill_fail_soft_and_audit_counts(pg_session):
    s = pg_session
    _seed_reports(s, _seed_instrument(s, "BFA"))
    _seed_instrument(s, "BFB")          # instrument without any earnings
    report = backfill_feature(
        s, SUE_FOS, clock=CLOCK, start=FROM, end=TO,
        symbols=["BFA", "BFB", "BFMISSING"])   # no instrument row -> fail-soft
    assert report.failed == ("BFMISSING",)
    assert "instrument rows" in report.failures[0]
    assert report.computed == {"BFA": 5, "BFB": 0}    # the run continued
    assert report.inserted == 5 and report.sessions == 5
    ev = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'quant.feature.materialized' "
        "ORDER BY seq DESC LIMIT 1")).scalar()
    assert ev["feature"] == "sue_foster_olsen_shevlin"
    assert ev["dataset_version"] == report.dataset_version
    assert ev["inserted"] == 5 and ev["symbols"] == 3
    assert ev["failed"] == ["BFMISSING"]
    assert ev["computed"] == {"BFA": 5, "BFB": 0}


def test_backfill_rejects_windows_without_sessions(pg_session):
    with pytest.raises(ValueError, match="no US trading sessions"):
        backfill_feature(pg_session, SUE_FOS, clock=CLOCK,
                         symbols=["BFA"],
                         start=date(2025, 1, 1),      # New Year's Day
                         end=date(2025, 1, 1))


def test_trading_universe_is_active_us_single_names(pg_session):
    s = pg_session
    _seed_instrument(s, "BFA")
    _seed_instrument(s, "BFETF", itype="etf")
    universe = trading_universe(s)
    assert "BFA" in universe
    assert "BFETF" not in universe      # ETFs excluded by construction


def _purge_committed_cli_rows(s):
    """Remove everything the CLI path commits (feature rows, XBFT fixture
    instruments and their earnings) so re-runs are deterministic and NOTHING
    leaks into the suite's shared instrument universe."""
    s.execute(text("TRUNCATE quant.feature_values"))
    s.execute(text("DELETE FROM quant.feature_definitions"))
    s.execute(text(
        "DELETE FROM market.earnings_surprises WHERE instrument_id IN "
        "(SELECT id FROM market.instruments WHERE exchange = 'XBFT')"))
    s.execute(text("DELETE FROM market.instruments WHERE exchange = 'XBFT'"))
    s.commit()


@pytest.fixture
def cli_env(monkeypatch, pg_session):
    """Point session_scope at the test DB; purge committed rows before AND
    after so counts are deterministic across re-runs (the CLI commits)."""
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    _purge_committed_cli_rows(pg_session)
    yield monkeypatch
    _purge_committed_cli_rows(pg_session)
    reset_app_engine()


def _argv(*symbols):
    return ["feature-backfill", "--feature", "sue_foster_olsen_shevlin",
            "--from", FROM.isoformat(), "--to", TO.isoformat(),
            "--symbols", ",".join(symbols),
            "--now", "2025-07-01T08:00:00+00:00"]


def test_cli_clean_rerun_and_failure_exit_codes(cli_env, capsys):
    with pg_scope() as s:   # committed => inactive (see _seed_instrument)
        _seed_reports(s, _seed_instrument(s, "BFA", active=False))

    cli_env.setattr(sys, "argv", _argv("BFA"))
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "5 values inserted" in out and "0 failed" in out

    # identical re-run: same inputs -> same dataset_version -> append-only no-op
    cli_env.setattr(sys, "argv", _argv("BFA"))
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "0 values inserted" in out and "5 already present" in out

    # a symbol without an instrument row: honest partial coverage, exit 2
    cli_env.setattr(sys, "argv", _argv("BFA", "BFMISSING"))
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 2
    out = capsys.readouterr().out
    assert "FAILURE: sue_foster_olsen_shevlin BFMISSING" in out
    assert "1 failed" in out


def test_cli_rejects_inverted_window(cli_env, capsys):
    cli_env.setattr(sys, "argv", [
        "feature-backfill", "--feature", "momentum_12_1",
        "--from", "2025-03-01", "--to", "2025-02-01"])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 2
    assert "after --to" in capsys.readouterr().err
