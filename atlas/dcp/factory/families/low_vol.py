"""The LOW-VOLATILITY feature family (Research Factory catalog widening,
reviewed 2026-07): one member, one hypothesis-bearing measure — no grid.

  low_vol_252 — NEGATIVE annualized realized volatility over the 252 sessions
                ending at t (population stdev of daily simple returns on
                split-adjusted closes, * sqrt(252), negated). Negative so the
                grammar's rank-desc puts the LOWEST-volatility names first;
                the sign is part of the pinned formula, not a runner option.

THE ECONOMIC LINE. The low-volatility anomaly: defensive stocks have earned
more than their risk-model due (Ang, Hodrick, Xing & Zhang 2006; Baker,
Bradley & Wurgler 2011 on benchmark-constrained managers; Frazzini & Pedersen
2014 betting-against-beta) — leverage-constrained investors overpay for
lottery-like volatility. Whether the LONG-ONLY top-5 expression clears
Atlas's ABSOLUTE bar (beat SPY buy-and-hold total return, ADR-0009) on this
panel is exactly what a counted recipe run exists to decide; the literature's
claim is risk-adjusted, so a FAIL against the raw-return gate is a live
possibility and would be an informative, honest verdict — the hypothesis
deserves a counted test, not an assumption in either direction.

WHY PRICE-BASED FIRST (and not value/quality): realized vol needs only the
same PIT bars momentum uses — delisted names covered, no restatement
look-ahead, no fundamentals-survivorship hole. The fundamentals families
remain blocked on honest point-in-time statement data and are NOT smuggled in
through today's payloads.

LINEAGE: 'low-vol' — a genuinely new research line (first registered trial
will deflate at its own count, per ADR-0016 the line can never be renamed to
reset its penalty). Declared in factory/features.FEATURE_LINEAGE in the same
reviewed diff, as the import guard requires.

The math lives in features/volatility.py (hashed here via code_paths, with
the adjustment module); an anchor test pins the compute at window=20
byte-identical to the production risk panel's vol_20d_ann on a split-bearing
series, so the family cannot drift from the vol convention Atlas already
reports.
"""
from __future__ import annotations

from pathlib import Path
from types import ModuleType

import atlas.dcp.features.volatility as _volatility
import atlas.dcp.market_data.adjustment as _adjustment
from atlas.dcp.features.store import FeatureDefinition
from atlas.dcp.features.volatility import (
    ANNUALIZATION,
    VENDOR_SOURCE,
    VOL_WINDOW,
    make_vol_compute,
    vol_extent,
)


def _src(module: ModuleType) -> Path:
    f = module.__file__
    assert f is not None, f"module {module.__name__} has no source file"
    return Path(f)


LOW_VOL_252 = FeatureDefinition(
    name="low_vol_252",
    version="1.0.0",
    market="US",
    spec={
        "formula": "-pstdev(close[i]/close[i-1] - 1 over window) * sqrt(252)",
        "window_sessions": VOL_WINDOW,
        "seasoning_sessions": VOL_WINDOW,
        "estimator": "population_stdev",
        "annualization_sessions": ANNUALIZATION,
        "adjustment": "split_adjusted_closes",
        "source": VENDOR_SOURCE,
        "carry_sessions": 0,
        "provenance": "low-volatility anomaly (Ang-Hodrick-Xing-Zhang 2006; "
                      "Baker-Bradley-Wurgler 2011; Frazzini-Pedersen 2014); "
                      "negative realized vol so rank-desc is defensive-first; "
                      "single member, no grid, no search",
    },
    code_paths=(Path(__file__), _src(_volatility), _src(_adjustment)),
    compute=make_vol_compute(VOL_WINDOW),
    input_extent=vol_extent,
)


def low_vol_members() -> dict[str, FeatureDefinition]:
    """The family's catalog contribution — deliberately a single member."""
    return {LOW_VOL_252.name: LOW_VOL_252}


# ADR-0016 lineage, declared INSIDE the family's hashed source (see
# families/momentum.py): renaming this line trips this member's pin.
FAMILY_LINEAGE: dict[str, str] = {
    "low_vol_252": "low-vol",
}
