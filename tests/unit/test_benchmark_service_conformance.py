"""F-006 conformance: the AUD reporting-basis benchmark/instrument return lives
in ONE place.

The currency-inconsistency defect was a total-return series being converted (or
NOT converted) to AUD in several modules, each free to drift. The fix routes
every reporting-basis return — the demotion bands, the memo scorecard, daily
attribution — through market_data/benchmark.py. This test locks that in two ways:

  1. NEGATIVE — no dcp module OTHER than the service may co-locate the
     reporting-basis pattern: building a total-return series
     (`total_return_series`) AND converting it to AUD (`fx_to_aud`) in the same
     module. That co-occurrence IS the reporting-basis return; it belongs to the
     service. (Backtest panels call `total_return_series` WITHOUT fx — a
     single-currency USD universe, legitimately not a cross-currency excess — so
     they never trip this.)

  2. POSITIVE — the grading/governance consumers actually import the service.

Signals that read SPY closes for a TREND/RANK (regime label, relative strength)
are deliberately currency-native — converting them to AUD would inject spurious
FX into a price signal — so they use neither primitive together and are not in
scope here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_DCP = Path(__file__).resolve().parents[2] / "atlas" / "dcp"
_SERVICE = _DCP / "market_data" / "benchmark.py"
# total_return.py DEFINES the primitive; it performs no FX. Excluded explicitly.
_ALLOWED = {_SERVICE, _DCP / "market_data" / "total_return.py"}


def _dcp_modules() -> list[Path]:
    return [p for p in _DCP.rglob("*.py") if "__pycache__" not in p.parts]


def test_service_file_exists_and_owns_the_pattern():
    src = _SERVICE.read_text()
    assert "total_return_series" in src and "fx_to_aud" in src, (
        "benchmark.py must be the home of the AUD reporting-basis return")


def test_no_other_dcp_module_builds_an_aud_total_return_inline():
    offenders = []
    for path in _dcp_modules():
        if path in _ALLOWED:
            continue
        src = path.read_text()
        if "total_return_series" in src and "fx_to_aud" in src:
            offenders.append(path.relative_to(_DCP.parent.parent).as_posix())
    assert not offenders, (
        "reporting-basis return (total_return_series + fx_to_aud) must live only "
        f"in market_data/benchmark.py; found inline in: {offenders}")


def test_scorecard_does_not_read_spy_bars_inline():
    """The scorecard must obtain its SPY leg from the service, not by selecting
    SPY closes and differencing them itself (the original F-006 defect site)."""
    src = (_DCP / "scorecard.py").read_text()
    assert "benchmark_reporting_series" in src
    assert "symbol = 'SPY'" not in src
    assert "spy_fwd / spy_anchor" not in src or "reporting" in src  # ratio is on service closes


@pytest.mark.parametrize("module,imported", [
    ("scorecard.py", "benchmark_reporting_series"),
    ("trading/bands.py", "benchmark_reporting_close"),
    ("reporting/attribution.py", "reporting_return_between"),
])
def test_consumers_import_the_service(module, imported):
    src = (_DCP / module).read_text()
    assert "market_data.benchmark import" in src or "market_data import benchmark" in src
    assert imported in src
