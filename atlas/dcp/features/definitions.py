"""The registered v1 features (ADR-0011 step 1): momentum_12_1 and
sue_foster_olsen_shevlin — the two factors already validated through the
gauntlet, migrated onto the store as DEFINITIONS (their generators and
backtests are untouched; equivalence tests prove the store is a faithful
substrate before anything depends on it).

Every definition pins its parameters (spec — stored as jsonb, so a definition
is auditable without reading code) and the ORDERED source files whose bytes
constitute the computation (code_paths -> code_sha). The pinned files include
the signal modules the math is imported from: editing signals/xsmom/v1.py
changes momentum_12_1's code_sha and register_feature will refuse to touch
the store until the change is reviewed as a redefinition — prompts-are-code,
applied to features.
"""
from __future__ import annotations

from pathlib import Path
from types import ModuleType

import atlas.dcp.features.momentum as _momentum
import atlas.dcp.features.sue as _sue
import atlas.dcp.market_data.adjustment as _adjustment
import atlas.dcp.signals.pead.v1 as _pead_v1
import atlas.dcp.signals.xsmom.v1 as _xsmom_v1
from atlas.dcp.features.store import FeatureDefinition
from atlas.dcp.signals.pead.v1 import (
    STALENESS_SESSIONS,
    STANDARDIZE_MIN,
    STANDARDIZE_WINDOW,
)
from atlas.dcp.signals.xsmom.v1 import LOOKBACK, SEASONING, SKIP


def _src(module: ModuleType) -> Path:
    f = module.__file__
    assert f is not None, f"module {module.__name__} has no source file"
    return Path(f)


MOMENTUM_12_1 = FeatureDefinition(
    name="momentum_12_1",
    version="1.0.0",
    market="US",
    spec={
        "formula": "close[t-skip] / close[t-lookback] - 1",
        "lookback_sessions": LOOKBACK,
        "skip_sessions": SKIP,
        "seasoning_sessions": SEASONING,
        "adjustment": "split_adjusted_closes",
        "source": _momentum.VENDOR_SOURCE,
        "carry_sessions": 0,
        "provenance": "signals/xsmom v1 formation math "
                      "(Jegadeesh & Titman 1993 12-1 momentum); no search",
    },
    code_paths=(_src(_momentum), _src(_xsmom_v1), _src(_adjustment)),
    compute=_momentum.compute_momentum,
    input_extent=_momentum.momentum_extent,
)

SUE_FOS = FeatureDefinition(
    name="sue_foster_olsen_shevlin",
    version="1.0.0",
    market="US",
    spec={
        "formula": "(epsActual - epsEstimate) / stdev(surprise over prior "
                   "8 reported quarters)",
        "standardize_window_quarters": STANDARDIZE_WINDOW,
        "standardize_min_quarters": STANDARDIZE_MIN,
        "staleness_sessions": STALENESS_SESSIONS,
        "variant": _sue.VARIANT,
        "eps_basis": "vendor backward-split-adjusted to current basis; used "
                     "directly (no on-read adjustment)",
        "representation": "dense carry-forward: pead live(t) stored at every "
                          "session where defined; no row where None",
        "carry_sessions": 0,
        "provenance": "signals/pead v1 (Foster-Olsen-Shevlin SUE; PEAD); "
                      "no search",
    },
    code_paths=(_src(_sue), _src(_pead_v1)),
    compute=_sue.compute_sue,
    input_extent=_sue.sue_extent,
)

FEATURES: dict[str, FeatureDefinition] = {
    f.name: f for f in (MOMENTUM_12_1, SUE_FOS)}


def get_feature(name: str) -> FeatureDefinition:
    try:
        return FEATURES[name]
    except KeyError:
        raise KeyError(f"unknown feature {name!r} — registered: "
                       f"{sorted(FEATURES)}") from None
