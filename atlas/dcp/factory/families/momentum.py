"""The BOUNDED momentum feature family (Research Factory phase 1; moved here
verbatim from factory/features.py in the families/ restructure — the math and
the pinned specs are unchanged, and the byte-identity tests still pin the
generalized compute at (252, 21) to features/momentum.compute_momentum).

  momentum_12_1  — THE canonical member: features/definitions.MOMENTUM_12_1
                   itself, REUSED BY IMPORT (identity, not a twin). Its math
                   is the production ranker's formation return, already proven
                   byte-identical to signals/xsmom/generate._formation_returns
                   by tests/integration/test_feature_equivalence_pg.py.
  momentum_6_1   — (lookback 126, skip 21)
  momentum_3_1   — (lookback  63, skip 21)
  momentum_12_0  — (lookback 252, skip  0)

The family members generalize features/momentum.py OPERATION FOR OPERATION
with (lookback, skip) as pinned per-definition parameters — same vendor pin,
same contiguity/fail-closed eligibility, same Decimal -> float per-leg
conversion, same split cap at action_date <= t. Each member is a DISTINCT
FeatureDefinition (own name, own pinned spec, code_sha over THIS module plus
the imported math modules), so register_feature's pin discipline applies per
member. The grid is a closed set — no constructor for arbitrary pairs, no
free-form formulas; widening it is a reviewed change to this file.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

import atlas.dcp.features.momentum as _momentum
import atlas.dcp.market_data.adjustment as _adjustment
import atlas.dcp.signals.xsmom.v1 as _xsmom_v1
from atlas.dcp.features.definitions import MOMENTUM_12_1
from atlas.dcp.features.momentum import (
    _CAL_SLACK_DAYS,
    VENDOR_SOURCE,
    momentum_extent,
)
from atlas.dcp.features.store import ComputeFn, FeatureDefinition
from atlas.dcp.market_data.adjustment import adjust_for_splits
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.market_data.models import Bar, Split

# The closed v1 grid: (lookback_sessions, skip_sessions). (252, 21) is the
# canonical momentum_12_1 and is served by the phase-1 definition itself.
MOMENTUM_GRID: Final[tuple[tuple[int, int], ...]] = (
    (252, 21), (126, 21), (63, 21), (252, 0))
_CANONICAL: Final[tuple[int, int]] = (252, 21)


def _src(module: ModuleType) -> Path:
    f = module.__file__
    assert f is not None, f"module {module.__name__} has no source file"
    return Path(f)


def family_member_name(lookback: int, skip: int) -> str:
    """momentum_<lookback months>_<skip months> — 21 sessions per month, the
    same convention that named momentum_12_1."""
    return f"momentum_{lookback // 21}_{skip // 21}"


def _make_compute(lookback: int, skip: int) -> ComputeFn:
    """features/momentum.py's compute_momentum with (LOOKBACK, SKIP)
    parameterized — the SAME operations in the SAME order (byte-identity at
    (252, 21) is pinned by test): Decimal vendor closes -> adjust_for_splits
    capped at t -> float() each leg -> float division c_skip / c_form - 1.0.
    Eligibility is the ranker's exact fail-closed rule: a close on EVERY of
    the lookback+1 US sessions ending at t, positive formation close."""
    window = lookback + 1
    skip_idx = window - 1 - skip

    def compute(db: Session, symbol: str, instrument_id: UUID,
                sessions: list[date]) -> dict[date, float]:
        if not sessions:
            return {}
        end = max(sessions)
        closes: dict[date, Decimal] = {}
        for r in db.execute(text(
                "SELECT bar_date, close FROM market.price_bars_daily "
                "WHERE instrument_id = :iid AND source = :src "
                "  AND close IS NOT NULL AND bar_date <= :end"),
                {"iid": instrument_id, "src": VENDOR_SOURCE, "end": end}):
            closes[r.bar_date] = Decimal(r.close)
        if not closes:
            return {}
        all_splits: list[Split] = [
            Split(symbol=symbol, action_date=r.action_date,
                  ratio=Decimal(r.ratio))
            for r in db.execute(text(
                "SELECT action_date, ratio FROM market.corporate_actions "
                "WHERE instrument_id = :iid AND action_type = 'split' "
                "  AND action_date <= :end ORDER BY action_date"),
                {"iid": instrument_id, "end": end})]

        out: dict[date, float] = {}
        for t in sorted(sessions):
            cal = trading_days_between(
                "US", t - timedelta(days=_CAL_SLACK_DAYS), t)
            if len(cal) < window or cal[-1] != t:
                continue                # not a US session / calendar too young
            win = cal[-window:]
            if any(d not in closes for d in win):
                continue                # gap in the window: fail closed
            probe = (win[0], win[skip_idx], win[-1])   # t-lookback, t-skip, t
            bars = [Bar(symbol=symbol, bar_date=d, open=closes[d],
                        high=closes[d], low=closes[d], close=closes[d],
                        volume=0) for d in probe]
            adj = adjust_for_splits(
                bars, [sp for sp in all_splits if sp.action_date <= t])
            c_form, c_skip = float(adj[0].close), float(adj[1].close)
            if c_form <= 0:
                continue                # unpriceable base: fail closed
            out[t] = c_skip / c_form - 1.0
        return out

    return compute


def _variant(lookback: int, skip: int) -> FeatureDefinition:
    """One family member: pinned spec (auditable without reading code) and
    code_sha over THIS module plus every module the math is imported from —
    the same code_paths discipline as the phase-1 definitions."""
    return FeatureDefinition(
        name=family_member_name(lookback, skip),
        version="1.0.0",
        market="US",
        spec={
            "formula": "close[t-skip] / close[t-lookback] - 1",
            "lookback_sessions": lookback,
            "skip_sessions": skip,
            "seasoning_sessions": lookback,
            "adjustment": "split_adjusted_closes",
            "source": VENDOR_SOURCE,
            "carry_sessions": 0,
            "provenance": "factory momentum family: momentum_12_1 math "
                          "(features/momentum.py) with (lookback, skip) "
                          "pinned per definition; closed grid, no search",
        },
        code_paths=(Path(__file__), _src(_momentum), _src(_xsmom_v1),
                    _src(_adjustment)),
        compute=_make_compute(lookback, skip),
        input_extent=momentum_extent,
    )


def momentum_members() -> dict[str, FeatureDefinition]:
    """The family's catalog contribution. The canonical (252, 21) is the
    phase-1 definition itself (identity, not a twin — registering it can
    never collide with phase 1); the variants are built here."""
    members: dict[str, FeatureDefinition] = {}
    for lookback, skip in MOMENTUM_GRID:
        if (lookback, skip) == _CANONICAL:
            members[MOMENTUM_12_1.name] = MOMENTUM_12_1
        else:
            member = _variant(lookback, skip)
            members[member.name] = member
    return members


# ADR-0016 lineage, declared INSIDE the family's hashed source: renaming this
# line changes THIS file's bytes, which changes every member's code_sha, which
# register_feature refuses until the change is reviewed and repinned — the
# deflation count can never be quietly reset from an unhashed aggregator.
FAMILY_LINEAGE: Final[dict[str, str]] = {
    "momentum_12_1": "momentum",
    "momentum_6_1": "momentum",
    "momentum_3_1": "momentum",
    "momentum_12_0": "momentum",
}
