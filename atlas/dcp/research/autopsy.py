"""FRAGILITY MARKERS ("pick autopsy") — a deterministic read of WHY a name looks
fragile, derived purely from the panels the dossier already computed (the
statistical models + Atlas's valuation). No new data, no LLM, no forecast: each
marker is a mechanical comparison of facts already on the page, rendered as a
plain-language reason.

This is the tool the HIMX post-mortem asked for: a momentum-only pick bought far
above its earnings-based worth, in technical breakdown, on collapsing margins,
fires a cluster of these markers at a glance.

WHAT IT IS NOT. Not a filter, not a score that touches capital, not advice. A
pick is MEASURED, never eliminated by code — any real filter that skewed the book
away from this profile would first have to clear the full gauntlet (null model,
deflated Sharpe, walk-forward) and a Principal signature. These markers only make
the fragile profile legible so a human can see the pattern across the graded
corpus.

Thresholds below are descriptive bands (documented, never tuned against any
outcome). Every marker fail-soft: a missing input simply does not fire.
"""
from __future__ import annotations

from typing import Any

# ---- descriptive threshold bands (not tuned; feed no signal) ----
_OVERVALUED_WARN = 0.20          # price this far above our central fair value
_OVERVALUED_ALERT = 0.40
_MOM_HIGH = 0.15                 # "high" 12-1 formation momentum
_REVERSAL_DROP = -0.10           # recent 20-session drop that marks a reversal
_BELOW_MA_WARN = -0.10           # price this far below its 50-day average
_BELOW_MA_ALERT = -0.20
_ROE_WARN = 0.08
_ROE_ALERT = 0.05
_REV_SHRINK = -0.01              # trailing revenue CAGR at/below this = shrinking
_RICH_PCTILE = 0.60             # earnings-multiple percentile that reads "expensive"
_CHEAP_PCTILE = 0.35            # sales/book percentile that reads "cheap"
_VOL_HIGH = 0.60                 # 20-day annualised vol
_BETA_HIGH = 2.5

_SEV_WEIGHT = {"alert": 3, "warn": 2, "info": 1}


def _num(x: object) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _get(d: object, *path: str) -> object:
    node: object = d
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _pct(x: float) -> str:
    return f"{x * 100:+.0f}%"


def _ord(x: float) -> str:
    """A percentile 0..1 as an ordinal string, e.g. 0.82 -> '82nd'."""
    n = int(round(x * 100))
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def compute_autopsy(models: dict[str, Any] | None,
                    valuation: dict[str, Any] | None) -> dict[str, Any]:
    """Derive the fragility markers for a name from its already-computed model +
    valuation panels. Returns the fired flags (most severe first), an overall
    fragility level, and a plain note. Everything fail-soft to 'no markers'."""
    flags: list[dict[str, str]] = []

    def fire(key: str, severity: str, label: str, detail: str) -> None:
        flags.append({"key": key, "severity": severity, "label": label, "detail": detail})

    price = _num(_get(models, "price")) or _num(_get(valuation, "price"))
    tech = _get(models, "technical") if models else None
    mom = _get(models, "momentum") if models else None
    risk = _get(models, "risk") if models else None
    quality = _get(models, "quality") if models else None

    # 1) overvalued vs our own earnings-based floor (EPV/DCF central)
    central = _num(_get(valuation, "summary", "fair_value_central"))
    if price and central and central > 0 and price > central:
        above = price / central - 1.0
        if above >= _OVERVALUED_WARN:
            sev = "alert" if above >= _OVERVALUED_ALERT else "warn"
            fire("overvalued", sev, "Above Atlas's fair-value floor",
                 f"trades {_pct(above)} above Atlas's central fair value "
                 f"(${central:.2f} vs ${price:.2f})")

    # 2) high formation momentum but a sharp recent reversal (a momentum crash)
    m121 = _num(_get(mom, "mom_12_1"))
    r20 = _num(_get(mom, "ret_20d"))
    if m121 is not None and r20 is not None and m121 > _MOM_HIGH and r20 <= _REVERSAL_DROP:
        fire("momentum_reversal", "alert", "Momentum in reversal",
             f"strong 12-1 momentum ({_pct(m121)}) but {_pct(r20)} over the last "
             f"20 sessions — a momentum name rolling over")

    # 3) broken trend — price well below its 50-day average
    px = price
    sma50 = _num(_get(tech, "sma_50"))
    if px and sma50 and sma50 > 0:
        rel = px / sma50 - 1.0
        if rel <= _BELOW_MA_WARN:
            sev = "alert" if rel <= _BELOW_MA_ALERT else "warn"
            fire("broken_trend", sev, "Below its 50-day trend",
                 f"price is {_pct(rel)} vs its 50-day average")

    # 4) Atlas's own technical read is bearish
    summary = _get(tech, "summary")
    if isinstance(summary, str) and summary in ("Sell", "Strong Sell"):
        sev = "alert" if summary == "Strong Sell" else "warn"
        bull, total = _get(tech, "bullish_signals"), _get(tech, "total_signals")
        extra = (f" ({bull}/{total} signals bullish)"
                 if isinstance(bull, int) and isinstance(total, int) else "")
        fire("technical", sev, f"Technical read: {summary}",
             f"Atlas's rules-based technical summary is {summary}{extra}")

    # 5) low profitability
    roe = _num(_get(quality, "roe"))
    if roe is None:
        roe = _num(_get(valuation, "dupont", "roe"))
    if roe is not None and roe < _ROE_WARN:
        sev = "alert" if roe < _ROE_ALERT else "warn"
        fire("low_quality", sev, "Low profitability",
             f"return on equity is only {roe * 100:.1f}%")

    # 6) shrinking fundamentals — trailing revenue actually contracting
    cagr = _num(_get(valuation, "dcf", "historical_revenue_cagr"))
    if cagr is not None and cagr <= _REV_SHRINK:
        fire("shrinking", "warn", "Revenue contracting",
             f"trailing revenue CAGR is {_pct(cagr)} — earnings power is shrinking, "
             f"not growing")

    # 7) the margin-collapse signature: expensive on earnings, cheap on sales/book
    pe_pct = _num(_get(valuation, "comparables", "multiples", "pe", "percentile"))
    ps_pct = _num(_get(valuation, "comparables", "multiples", "ps", "percentile"))
    pb_pct = _num(_get(valuation, "comparables", "multiples", "pb", "percentile"))
    cheap_leg = min([p for p in (ps_pct, pb_pct) if p is not None], default=None)
    if pe_pct is not None and pe_pct >= _RICH_PCTILE \
            and cheap_leg is not None and cheap_leg <= _CHEAP_PCTILE:
        fire("margin_collapse", "warn", "Margin-collapse signature",
             f"expensive on earnings (P/E {_ord(pe_pct)} percentile of its sector) "
             f"yet cheap on sales/book ({_ord(cheap_leg)} percentile) — a sign "
             f"margins have collapsed, not that it is cheap")

    # 8) high volatility / beta — a fragile carrier even when right
    vol = _num(_get(risk, "vol_20d_ann"))
    beta = _num(_get(risk, "beta_vs_spy"))
    if (vol is not None and vol > _VOL_HIGH) or (beta is not None and beta > _BETA_HIGH):
        bits = []
        if vol is not None:
            bits.append(f"{vol * 100:.0f}% annualised vol")
        if beta is not None:
            bits.append(f"beta {beta:.1f}")
        fire("high_volatility", "warn", "Very high volatility",
             " · ".join(bits) + " — large moves in both directions")

    flags.sort(key=lambda f: -_SEV_WEIGHT.get(f["severity"], 0))
    n_alert = sum(1 for f in flags if f["severity"] == "alert")
    n_warn = sum(1 for f in flags if f["severity"] == "warn")
    level = ("fragile" if (n_alert >= 2 or (n_alert >= 1 and n_warn >= 2))
             else "caution" if (n_alert >= 1 or n_warn >= 2)
             else "clear")
    return {
        "flags": flags, "n_alerts": n_alert, "n_warns": n_warn, "level": level,
        "note": ("Descriptive markers Atlas computes from the panels above — NOT a "
                 "filter and NOT advice. A pick is measured, never eliminated by "
                 "code; any filter on this profile must clear the gauntlet first."),
    }
