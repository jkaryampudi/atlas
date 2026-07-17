"""Percentile tolerance-band derivation from the backtest equity curve —
board-memo item 7, the approval-contract hardening ADR-0010/0013 promised:
"the approval-contract build must derive exact percentile bands from the
stored equity curve and may only TIGHTEN these".

THE MATH (pure, DB-free; the one-shot tool atlas/tools/derive_bands.py feeds
it regenerated curves):

- EXCESS FLOOR: the trailing-126-session strategy-minus-SPY excess (pp) is
  computed across ALL overlapping 126-session windows of the backtest —
  excess[t] = (c[t]/c[t-126] - 1)*100 - (spy[t]/spy[t-126] - 1)*100 — the
  exact statistic the daily band check enforces (bands.py: value_t vs the
  126th prior stored row). The derived floor is the 1st percentile of that
  distribution: a live sleeve reading below it is doing worse than 99% of
  the backtest's own history. Percentile convention: the committed type-7
  linear-interpolation `percentile` (xsmom_run), never restated.

- DD FLOOR: min(full-backtest max drawdown, 1st percentile of the rolling
  252-session-window max-DD distribution) x DD_MARGIN. The full-window DD
  mathematically dominates any windowed DD (its running peak is global), so
  the min() is a belt-and-braces guard, kept because the spec names both
  legs. WHY THE x1.1 MARGIN: the paper sleeve is an implementable variant on
  a different universe (ADR-0007) with FX, lifecycle accounting and
  realised-PnL steps the backtest curve does not carry (bands.py documents
  the conservative liquidation-step artefact); a floor set exactly AT the
  backtest's record would demote the sleeve the first time it merely
  EQUALLED history. 10% beyond the record keeps the band a hard bound on
  "outside the validated record" without hair-triggering on artefacts of
  the accounting difference. The margin is a constant, recorded verbatim in
  the derivation artifact, and the tighten-only rule still applies after it.

- TIGHTEN-ONLY (enforced HERE, in code, not by reviewer discipline): the
  derived value replaces the standing/provisional one iff it is STRICTER
  (closer to zero). A derivation that would loosen a band keeps the standing
  value and records the refusal verbatim in the proposed jsonb — loosening
  any band requires a new signed ADR (ADR-0010 §guardrails), and this module
  is structurally unable to do it. The apply tool re-checks the same rule
  against the stored row before writing (defense in depth).

- CUSUM PARAMETERS (drift early-warning, consumed by bands.check_cusum):
  mean and population sigma of the backtest's DAILY strategy-minus-SPY
  excess, plus the classic k=0.5σ slack / h=5σ threshold the committed
  CusumDetector (dcp/learning/drift.py) documents. Stored inside the same
  tolerance_bands jsonb so the daily check needs no second artifact. A
  CUSUM breach PAGES; it never demotes — demotion authority stays with the
  two percentile bands (see bands.check_cusum for the signed rationale).

The output `tolerance_bands` dict is the full proposed jsonb: provisional
false, both chosen bands, the embedded derivation record (percentile, window
counts, curve sha256, per-band decisions verbatim) and the cusum block.
"""
from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

from atlas.dcp.backtest.xsmom_run import percentile
from atlas.dcp.trading.bands import DD_BAND_KEY, EXCESS_BAND_KEY, EXCESS_SESSIONS

PERCENTILE = 0.01     # "worse than 99% of the backtest's own history"
DD_WINDOW = 252       # rolling max-DD window (sessions) — one trading year
DD_MARGIN = 1.1       # WHY: see module docstring (implementable-variant slack)
CUSUM_K_SIGMA = 0.5   # slack per observation, in daily-excess sigmas
CUSUM_H_SIGMA = 5.0   # decision threshold, in daily-excess sigmas
CUSUM_KEY = "cusum"
BAND_KEYS = (DD_BAND_KEY, EXCESS_BAND_KEY)


@dataclass(frozen=True)
class BandDecision:
    """One band's tighten-only verdict, recorded verbatim."""
    key: str
    provisional: float    # the standing value (ADR provisional or prior derived)
    derived: float
    chosen: float
    tightened: bool       # True = derived replaced the standing value
    note: str


@dataclass(frozen=True)
class DerivedBands:
    excess_windows: int
    dd_windows: int
    derived_excess_floor_pp: float
    full_max_dd: float
    rolling_dd_p1: float
    derived_dd_floor: float
    mean_daily_excess: float
    sigma_daily_excess: float
    decisions: tuple[BandDecision, ...]
    tolerance_bands: dict[str, Any]       # the full proposed jsonb

    def decision(self, key: str) -> BandDecision:
        for d in self.decisions:
            if d.key == key:
                return d
        raise KeyError(key)


def max_drawdown(curve: Sequence[float]) -> float:
    """Max drawdown from the running peak over the whole curve (<= 0)."""
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    return mdd


def trailing_excess_distribution(strategy: Sequence[float],
                                 spy: Sequence[float], *,
                                 window: int = EXCESS_SESSIONS) -> list[float]:
    """Trailing `window`-session strategy-minus-SPY excess in pp, for ALL
    overlapping windows: t = window .. n-1 (n - window values). Mirrors the
    daily check's statistic (bands.py: value_t vs the `window`-th prior row).
    """
    if len(strategy) != len(spy):
        raise ValueError("strategy and SPY curves must cover the same sessions")
    n = len(strategy)
    if n <= window:
        raise ValueError(f"curve too short: {n} sessions cannot form a single "
                         f"{window}-session window")
    return [(strategy[t] / strategy[t - window] - 1.0) * 100.0
            - (spy[t] / spy[t - window] - 1.0) * 100.0
            for t in range(window, n)]


def rolling_max_dd_distribution(curve: Sequence[float], *,
                                window: int = DD_WINDOW) -> list[float]:
    """Max drawdown inside each rolling `window`-session span (window+1
    points, starts i = 0 .. n-window-1). Empty when the curve is shorter
    than one span — the caller then falls back to the full-window DD."""
    n = len(curve)
    return [max_drawdown(curve[i:i + window + 1]) for i in range(n - window)]


def curve_sha256(dates: Sequence[date], strategy: Sequence[float],
                 spy: Sequence[float]) -> str:
    """Deterministic fingerprint of the exact curves a derivation consumed —
    lands in the artifact so a re-derivation is checkable byte-for-byte."""
    h = hashlib.sha256()
    for d, c, s in zip(dates, strategy, spy, strict=True):
        h.update(f"{d.isoformat()}:{c!r}:{s!r};".encode())
    return h.hexdigest()


def _standing_band(provisional: Mapping[str, Any], key: str) -> float:
    try:
        v = float(provisional[key])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"provisional bands missing or malformed {key}: "
                         f"{e!r}") from e
    if v >= 0:
        raise ValueError(f"provisional band {key}={v} is not a negative "
                         "floor — refusing a nonsensical standing band")
    return v


def _decide(key: str, derived: float, standing: float) -> BandDecision:
    """TIGHTEN-ONLY, in code: bands are floors below zero, so stricter =
    closer to zero. Looser derivations never replace the standing value —
    they are recorded verbatim; loosening requires a new signed ADR."""
    if derived > standing:
        return BandDecision(
            key=key, provisional=standing, derived=derived, chosen=derived,
            tightened=True,
            note=(f"derived {key}={derived:.6f} is STRICTER than standing "
                  f"{standing:.6f} — replaced (tighten-only, board item 7)"))
    return BandDecision(
        key=key, provisional=standing, derived=derived, chosen=standing,
        tightened=False,
        note=(f"derived {key}={derived:.6f} would LOOSEN standing "
              f"{standing:.6f} — standing value KEPT verbatim; loosening any "
              "band requires a new signed ADR (ADR-0010/0013 tighten-only)"))


def derive_proposed_bands(*, dates: Sequence[date],
                          strategy_curve: Sequence[float],
                          spy_curve: Sequence[float],
                          provisional: Mapping[str, Any],
                          curve_note: str,
                          excess_window: int = EXCESS_SESSIONS,
                          dd_window: int = DD_WINDOW) -> DerivedBands:
    """Derive the exact percentile bands and the CUSUM parameters from one
    backtest curve pair, apply the tighten-only rule against the standing
    bands, and return the full proposed tolerance_bands jsonb."""
    n = len(strategy_curve)
    if not (n == len(spy_curve) == len(dates)):
        raise ValueError("strategy, SPY and dates must cover the same sessions")
    if any(v <= 0 for v in strategy_curve) or any(v <= 0 for v in spy_curve):
        raise ValueError("equity curves must be strictly positive")
    standing_dd = _standing_band(provisional, DD_BAND_KEY)
    standing_ex = _standing_band(provisional, EXCESS_BAND_KEY)

    excess_dist = trailing_excess_distribution(strategy_curve, spy_curve,
                                               window=excess_window)
    excess_floor = percentile(excess_dist, PERCENTILE)

    full_dd = max_drawdown(strategy_curve)
    dd_dist = rolling_max_dd_distribution(strategy_curve, window=dd_window)
    # full-window DD dominates any windowed DD (global running peak); the
    # min() keeps both legs of the specified formula explicit anyway
    rolling_p1 = percentile(dd_dist, PERCENTILE) if dd_dist else full_dd
    dd_floor = min(full_dd, rolling_p1) * DD_MARGIN

    daily_excess = [(strategy_curve[t] / strategy_curve[t - 1] - 1.0)
                    - (spy_curve[t] / spy_curve[t - 1] - 1.0)
                    for t in range(1, n)]
    mu = statistics.fmean(daily_excess)
    sigma = statistics.pstdev(daily_excess)
    if sigma <= 0:
        raise ValueError("zero-variance daily excess — CUSUM cannot be "
                         "parameterised; refusing a degenerate contract")

    dd_dec = _decide(DD_BAND_KEY, dd_floor, standing_dd)
    ex_dec = _decide(EXCESS_BAND_KEY, excess_floor, standing_ex)

    tolerance_bands: dict[str, Any] = {
        "provisional": False,
        "demote_to": str(provisional.get("demote_to", "suspended")),
        DD_BAND_KEY: dd_dec.chosen,
        EXCESS_BAND_KEY: ex_dec.chosen,
        "derivation": {
            "source": "board item 7 approval-contract derivation "
                      "(tighten-only; ADR-0010/ADR-0013 guardrails)",
            "percentile": PERCENTILE,
            "excess_window_sessions": excess_window,
            "excess_windows": len(excess_dist),
            "derived_excess_floor_pp": excess_floor,
            "dd_window_sessions": dd_window,
            "dd_windows": len(dd_dist),
            "full_backtest_max_dd": full_dd,
            "rolling_dd_p1": rolling_p1,
            "dd_margin": DD_MARGIN,
            "derived_dd_floor": dd_floor,
            "sessions": n,
            "window": f"{dates[0]}..{dates[-1]}",
            "curve_sha256": curve_sha256(dates, strategy_curve, spy_curve),
            "curve_note": curve_note,
            "decisions": {
                d.key: {"provisional": d.provisional, "derived": d.derived,
                        "chosen": d.chosen, "tightened": d.tightened,
                        "note": d.note}
                for d in (dd_dec, ex_dec)},
        },
        CUSUM_KEY: {
            "k_sigma": CUSUM_K_SIGMA,
            "h_sigma": CUSUM_H_SIGMA,
            "mean_daily_excess": mu,
            "sigma_daily_excess": sigma,
            "action_on_breach": "page-only — Principal review; demotion "
                                "authority stays with the two tolerance "
                                "bands (a CUSUM auto-demote would need its "
                                "own signed criterion)",
        },
    }
    return DerivedBands(
        excess_windows=len(excess_dist), dd_windows=len(dd_dist),
        derived_excess_floor_pp=excess_floor, full_max_dd=full_dd,
        rolling_dd_p1=rolling_p1, derived_dd_floor=dd_floor,
        mean_daily_excess=mu, sigma_daily_excess=sigma,
        decisions=(dd_dec, ex_dec), tolerance_bands=tolerance_bands)
