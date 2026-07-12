"""CLI entrypoint contracts (review finding: main() and the exit-2-on-red
contract were entirely untested). Runs against atlas_test via env override."""
from __future__ import annotations

import sys

import pytest

from atlas.dcp.market_data.backfill import main as backfill_main
from atlas.dcp.market_data.fx import main as fx_main
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg


@pytest.fixture
def cli_env(monkeypatch, pg_session):
    """Point session_scope at atlas_test and force the fixture adapter."""
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    monkeypatch.setenv("ATLAS_EODHD_API_KEY", "")  # never hit the real vendor here
    reset_app_engine()  # drop the cached engine AND dispose its pool
    yield monkeypatch
    reset_app_engine()


def test_backfill_cli_clean_week_exits_zero(cli_env, capsys):
    cli_env.setattr(sys, "argv", ["backfill", "--years", "0.013",
                                  "--end", "2024-07-15", "--market", "US"])
    with pytest.raises(SystemExit) as e:
        backfill_main()
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "zero red gates" in out


def test_backfill_cli_explicit_from_window_exits_zero(cli_env, capsys):
    """Deep-history mode: --from replaces --years with an explicit ISO start
    (same fixture week here; the operator points it at 2010-01-01 for real).
    The report now prints per-instrument inception dates (rules v1.2)."""
    cli_env.setattr(sys, "argv", ["backfill", "--from", "2024-07-10",
                                  "--end", "2024-07-15", "--market", "US"])
    with pytest.raises(SystemExit) as e:
        backfill_main()
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "zero red gates" in out
    assert "inception AVGO: 2024-07-10" in out


def test_backfill_cli_from_and_years_are_mutually_exclusive(cli_env, capsys):
    cli_env.setattr(sys, "argv", ["backfill", "--from", "2010-01-01",
                                  "--years", "2", "--end", "2026-07-10"])
    with pytest.raises(SystemExit) as e:
        backfill_main()
    assert e.value.code == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_backfill_cli_from_after_end_is_rejected(cli_env, capsys):
    cli_env.setattr(sys, "argv", ["backfill", "--from", "2026-07-11",
                                  "--end", "2026-07-10"])
    with pytest.raises(SystemExit) as e:
        backfill_main()
    assert e.value.code == 2
    assert "after --end" in capsys.readouterr().err


def test_backfill_cli_red_market_exits_two(cli_env, capsys):
    cli_env.setattr(sys, "argv", ["backfill", "--years", "0.013",
                                  "--end", "2024-07-15", "--market", "AU"])
    with pytest.raises(SystemExit) as e:
        backfill_main()
    assert e.value.code == 2
    assert "FAILURES PRESENT" in capsys.readouterr().out


def test_fx_cli_weekday_missing_rate_exits_two(cli_env, capsys):
    # Wednesday 2026-07-08: no fixture rate -> incident, not quiet success
    cli_env.setattr(sys, "argv", ["fx", "--date", "2026-07-08"])
    with pytest.raises(SystemExit) as e:
        fx_main()
    assert e.value.code == 2
    assert "MISSING" in capsys.readouterr().out


def test_fx_cli_rate_present_succeeds(cli_env, capsys):
    cli_env.setattr(sys, "argv", ["fx", "--date", "2026-07-10"])
    fx_main()  # no SystemExit on success
    assert "wrote 1 rate(s)" in capsys.readouterr().out
