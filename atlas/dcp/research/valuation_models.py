"""Atlas's OWN deterministic valuation models for the research dossier — a
mechanical fair-value estimate built from data we already store, to fill the
slots a pro-research report fills with PROPRIETARY numbers we neither have nor
copy (their "Fair Value", their "Financial Health" score).

WHAT THIS IS. Four independent, textbook valuation methods, each computed
point-in-time and deterministically from the stock's reported financials, its
sector peers in our universe, and its price:

  * CAPM / WACC          — the discount rate (cost of equity + after-tax debt).
  * EPV (Earnings Power)  — Greenwald's NO-GROWTH earnings floor: what current
                            earnings power is worth if it never grows. A floor,
                            not a target — deliberately conservative.
  * DCF (2-stage)         — a discounted-cash-flow fair value, presented as a
                            TRANSPARENT SENSITIVITY GRID over growth × WACC (not
                            a single number), with the central growth derived
                            MECHANICALLY from the company's own revenue history.
  * Comparables           — sector-peer median multiples (P/E, EV/EBITDA, P/S,
                            P/B) applied to the stock's own metrics, plus the
                            stock's PERCENTILE within its sector.

Then a fair-value RANGE across the methods and Atlas's own verdict (price is
below / within / above our model range).

HONESTY (Constitution: no invented numbers).
  * Every input is a reported fact or a mechanical function of reported facts.
  * The only free parameters are the CAPM constants (risk-free rate, equity
    risk premium) and the terminal growth — declared at module top and RETURNED
    in the output as explicit assumptions the reader can see and mentally flex.
    They are NOT tuned against any outcome and feed no signal.
  * Growth is not fabricated: DCF near-term growth is the company's own trailing
    revenue CAGR, capped so runaway extrapolation cannot masquerade as truth,
    and the full grid shows fair value at 0 %..cap growth regardless.
  * These are educational mechanical valuations, NOT price targets, and — like
    everything in research.source_picks — MEASURED, NEVER APPLIED: nothing here
    reaches sizing / pricing / execution.

POINT-IN-TIME. Bounded to `as_of`: the fundamentals snapshot is the latest with
as_of <= as_of; statement periods and the price are filtered to <= as_of; peer
multiples are each peer's latest snapshot <= as_of. No look-ahead.

NUMERIC-ONLY CHOKE. Every payload read goes through the same numeric choke used
for agent evidence (fundamentals._number, via financials_panel._num). Only
numbers leave this module.
"""
from __future__ import annotations

import math
import statistics
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.dcp.market_data.fundamentals import _get
from atlas.dcp.research.financials_panel import _num, _period_date

# ---- declared assumptions (surfaced in the output; not tuned, feed no signal) ----
RISK_FREE_RATE = 0.04            # long-run nominal risk-free proxy
EQUITY_RISK_PREMIUM = 0.05       # market equity risk premium
TERMINAL_GROWTH = 0.025          # perpetuity growth (~long-run nominal GDP)
STATUTORY_TAX = 0.21             # US federal corporate rate — the tax fallback
_SANE_TAX = (0.0, 0.35)          # effective-tax band; outside it we normalise
DCF_YEARS = 5                    # explicit-forecast horizon
GROWTH_CAP = 0.15                # cap on extrapolated near-term growth
_MIN_WACC_SPREAD = 0.01          # WACC must exceed terminal growth by this much
_DEFAULT_BETA = 1.0              # only if no beta is available at all
VENDOR_SOURCE = "EodhdAdapter"


def _years(payload: dict[str, object], section: str,
           as_of: date, n: int) -> list[dict[str, object]]:
    """Ascending yearly rows of one statement section, point-in-time bounded to
    period-end <= as_of, most-recent `n` kept. Empty when absent."""
    sec = _get(payload, ("Financials", section, "yearly"))
    if not isinstance(sec, dict):
        return []
    dated: list[tuple[date, dict[str, object]]] = []
    for _k, row in sec.items():
        pend = _period_date(row)
        if pend is not None and pend <= as_of and isinstance(row, dict):
            dated.append((pend, row))
    dated.sort(key=lambda t: t[0])
    return [r for _d, r in dated[-n:]]


def _latest(payload: dict[str, object], section: str, key: str,
            as_of: date) -> float | None:
    rows = _years(payload, section, as_of, 1)
    return _num(rows[-1].get(key)) if rows else None


def _effective_tax(payload: dict[str, object], as_of: date) -> float:
    """Effective tax rate from the latest year, NORMALISED: a rate outside the
    sane band (e.g. AMD's negative rate from a one-off tax benefit, or >35 %) is
    replaced by the statutory rate so a transient tax item cannot distort a
    normalised valuation. incomeBeforeTax <= 0 also falls back to statutory."""
    pre_tax = _latest(payload, "Income_Statement", "incomeBeforeTax", as_of)
    tax = _latest(payload, "Income_Statement", "incomeTaxExpense", as_of)
    if pre_tax is None or tax is None or pre_tax <= 0:
        return STATUTORY_TAX
    eff = tax / pre_tax
    return eff if _SANE_TAX[0] <= eff <= _SANE_TAX[1] else STATUTORY_TAX


def _net_debt(payload: dict[str, object], as_of: date) -> float | None:
    """Net debt (debt minus cash) from the latest balance sheet. Prefers the
    vendor `netDebt` fact; falls back to total debt minus cash & ST investments.
    NEGATIVE means net cash — which correctly ADDS to equity value downstream."""
    nd = _latest(payload, "Balance_Sheet", "netDebt", as_of)
    if nd is not None:
        return nd
    debt = _latest(payload, "Balance_Sheet", "shortLongTermDebtTotal", as_of)
    cash = _latest(payload, "Balance_Sheet", "cashAndShortTermInvestments", as_of)
    # both legs required — a missing debt OR cash line is honest absence, not a
    # zero we invent (downstream methods fail-soft on a None net-debt).
    if debt is None or cash is None:
        return None
    return debt - cash


def _shares(payload: dict[str, object]) -> float | None:
    s = _num(_get(payload, ("SharesStats", "SharesOutstanding")))
    return s if (s is not None and s > 0) else None


def _beta(payload: dict[str, object]) -> float | None:
    return _num(_get(payload, ("Technicals", "Beta")))


def _latest_close(session: Session, instrument_id: str, as_of: date) -> float | None:
    v = session.execute(text(
        "SELECT close FROM market.price_bars_daily WHERE instrument_id = :i "
        "  AND source = :src AND close IS NOT NULL AND bar_date <= :d "
        "ORDER BY bar_date DESC LIMIT 1"),
        {"i": instrument_id, "src": VENDOR_SOURCE, "d": as_of}).scalar()
    return float(v) if v is not None else None


# ---------------------------------------------------------------------------
# Cost of capital (CAPM equity + after-tax debt -> WACC)
# ---------------------------------------------------------------------------

def _cost_of_capital(payload: dict[str, object], as_of: date, *,
                     market_cap: float | None, tax: float) -> dict[str, object]:
    beta = _beta(payload)
    beta_used = beta if beta is not None else _DEFAULT_BETA
    cost_equity = RISK_FREE_RATE + beta_used * EQUITY_RISK_PREMIUM

    debt = _latest(payload, "Balance_Sheet", "shortLongTermDebtTotal", as_of)
    interest = _latest(payload, "Income_Statement", "interestExpense", as_of)
    cost_debt = (abs(interest) / debt) if (interest is not None and debt and debt > 0) else None
    cost_debt_at = (cost_debt * (1.0 - tax)) if cost_debt is not None else None

    wacc: float | None = None
    if market_cap and market_cap > 0:
        e = market_cap
        # only carry the debt weight when its cost is actually known — otherwise
        # WACC degrades to the (higher, conservative) all-equity cost of equity
        # rather than blending debt in at a fabricated 0 %.
        d = debt if (debt and debt > 0 and cost_debt_at is not None) else 0.0
        v = e + d
        rd = cost_debt_at if cost_debt_at is not None else 0.0
        wacc = (e / v) * cost_equity + (d / v) * rd
    return {
        "beta": beta, "beta_used": beta_used,
        "cost_of_equity": cost_equity,
        "cost_of_debt_pretax": cost_debt,
        "cost_of_debt_aftertax": cost_debt_at,
        "wacc": wacc,
        "assumptions": {
            "risk_free_rate": RISK_FREE_RATE,
            "equity_risk_premium": EQUITY_RISK_PREMIUM,
            "tax_rate": tax,
        },
    }


# ---------------------------------------------------------------------------
# EPV — Earnings Power Value (no-growth floor, Greenwald)
# ---------------------------------------------------------------------------

def _epv(payload: dict[str, object], as_of: date, *, revenue_ttm: float | None,
         net_debt: float | None, shares: float | None, wacc: float | None,
         tax: float, price: float | None) -> dict[str, object]:
    """No-growth earnings power: normalised NOPAT capitalised at WACC. Normalised
    operating margin is the mean of up-to-5 annual operating margins (smoothing
    the cycle); applied to TTM revenue -> normalised EBIT -> after-tax NOPAT ->
    perpetuity value / WACC -> less net debt -> per share. A conservative FLOOR:
    it credits ZERO growth by construction."""
    rows = _years(payload, "Income_Statement", as_of, 5)
    margins: list[float] = []
    for r in rows:
        rev = _num(r.get("totalRevenue"))
        oi = _num(r.get("operatingIncome"))
        if rev and rev > 0 and oi is not None:
            margins.append(oi / rev)
    out: dict[str, object] = {
        "normalized_operating_margin": None, "normalized_ebit": None,
        "nopat": None, "enterprise_value": None, "equity_value": None,
        "fair_value_per_share": None, "upside_pct": None,
        "note": "no-growth earnings floor — credits zero growth by construction",
    }
    if not margins or revenue_ttm is None or wacc is None or wacc <= 0 \
            or net_debt is None or shares is None:
        return out
    norm_margin = statistics.fmean(margins)
    norm_ebit = norm_margin * revenue_ttm
    nopat = norm_ebit * (1.0 - tax)
    ev = nopat / wacc
    equity = ev - net_debt
    fv = equity / shares
    out.update({
        "normalized_operating_margin": norm_margin, "normalized_ebit": norm_ebit,
        "nopat": nopat, "enterprise_value": ev, "equity_value": equity,
        "fair_value_per_share": fv,
        "upside_pct": (fv / price - 1.0) if price else None,
    })
    return out


# ---------------------------------------------------------------------------
# DCF — 2-stage, presented as a sensitivity grid
# ---------------------------------------------------------------------------

def _dcf_value(base_fcf: float, g1: float, wacc: float, g_term: float,
               net_debt: float, shares: float) -> float | None:
    """One 2-stage DCF: FCF grown at g1 for DCF_YEARS, Gordon terminal at
    g_term, all discounted at wacc; less net debt; per share. Undefined (None)
    unless wacc exceeds g_term by the minimum spread."""
    if wacc - g_term < _MIN_WACC_SPREAD:
        return None
    pv = 0.0
    fcf = base_fcf
    for yr in range(1, DCF_YEARS + 1):
        fcf = fcf * (1.0 + g1)
        pv += fcf / ((1.0 + wacc) ** yr)
    terminal = fcf * (1.0 + g_term) / (wacc - g_term)
    pv += terminal / ((1.0 + wacc) ** DCF_YEARS)
    equity = pv - net_debt
    return equity / shares


def _revenue_cagr(payload: dict[str, object], as_of: date) -> float | None:
    """Trailing revenue CAGR across available annual years (first vs last),
    or None if fewer than two positive-revenue years exist."""
    rows = _years(payload, "Income_Statement", as_of, 6)
    revs: list[tuple[date, float]] = []
    for r in rows:
        d = _period_date(r)
        v = _num(r.get("totalRevenue"))
        if d is not None and v is not None and v > 0:
            revs.append((d, v))
    if len(revs) < 2:
        return None
    first, last = revs[0][1], revs[-1][1]
    years = revs[-1][0].year - revs[0][0].year
    if years <= 0 or first <= 0:
        return None
    return float((last / first) ** (1.0 / years)) - 1.0


def _dcf(payload: dict[str, object], as_of: date, *, net_debt: float | None,
         shares: float | None, wacc: float | None, tax: float,
         price: float | None) -> dict[str, object]:
    # EODHD freeCashFlow is CFO - Capex — a POST-interest (levered) figure. To
    # discount at WACC and bridge to equity with net debt (the FCFF/enterprise
    # framework) the input must be UNLEVERED, so add back after-tax interest:
    # FCFF = (CFO - Capex) + interest * (1 - tax). Without this, financing cost
    # is double-counted (once inside FCF, once via the net-debt bridge).
    levered_fcf = _latest(payload, "Cash_Flow", "freeCashFlow", as_of)
    interest = _latest(payload, "Income_Statement", "interestExpense", as_of)
    base_fcf = levered_fcf
    if levered_fcf is not None and interest is not None:
        base_fcf = levered_fcf + abs(interest) * (1.0 - tax)
    hist_cagr = _revenue_cagr(payload, as_of)
    # central near-term growth: the company's own trailing revenue CAGR, floored
    # at 0 and capped so runaway extrapolation cannot masquerade as a fact.
    central_g = None if hist_cagr is None else max(0.0, min(hist_cagr, GROWTH_CAP))
    out: dict[str, object] = {
        "base_fcf": base_fcf, "levered_fcf": levered_fcf,
        "historical_revenue_cagr": hist_cagr, "central_growth": central_g,
        "terminal_growth": TERMINAL_GROWTH, "forecast_years": DCF_YEARS,
        "wacc": wacc, "fair_value_per_share": None, "upside_pct": None,
        "sensitivity": [],
        "note": ("unlevered FCFF discounted at WACC; assumption-sensitive — the "
                 "grid shows fair value across growth × WACC"),
    }
    if base_fcf is None or base_fcf <= 0 or net_debt is None or shares is None \
            or wacc is None or wacc <= 0:
        return out
    grid: list[dict[str, object]] = []
    for g1 in (0.0, 0.05, 0.10, GROWTH_CAP):
        for w in (wacc - 0.01, wacc, wacc + 0.01):
            fv = _dcf_value(base_fcf, g1, w, TERMINAL_GROWTH, net_debt, shares)
            grid.append({"growth": g1, "wacc": w, "fair_value_per_share": fv})
    out["sensitivity"] = grid
    if central_g is not None:
        central = _dcf_value(base_fcf, central_g, wacc, TERMINAL_GROWTH, net_debt, shares)
        out["fair_value_per_share"] = central
        out["upside_pct"] = (central / price - 1.0) if (central and price) else None
    return out


# ---------------------------------------------------------------------------
# Comparables — sector-peer median multiples + percentile
# ---------------------------------------------------------------------------

_MULTIPLES = (
    ("pe", ("Valuation", "TrailingPE")),
    ("ev_ebitda", ("Valuation", "EnterpriseValueEbitda")),
    ("ps", ("Valuation", "PriceSalesTTM")),
    ("pb", ("Valuation", "PriceBookMRQ")),
)


def _peer_multiples(session: Session, instrument_id: str, sector: str | None,
                    as_of: date) -> tuple[dict[str, list[float]], int]:
    """Latest-<=-as_of vendor valuation multiples of every OTHER active US
    single name in the same GICS sector. Only positive, finite values are kept
    (a negative P/E or P/B is not a meaningful comparable). Also returns the
    count of peers that carried at least one usable multiple — the honest
    denominator, since each multiple has its own valid-value count."""
    out: dict[str, list[float]] = {k: [] for k, _ in _MULTIPLES}
    if not sector:
        return out, 0
    rows = session.execute(text(
        "SELECT f.payload FROM market.instruments i "
        "JOIN LATERAL (SELECT payload FROM market.fundamentals f2 "
        "              WHERE f2.instrument_id = i.id AND f2.as_of <= :on "
        "              ORDER BY f2.as_of DESC LIMIT 1) f ON true "
        "WHERE i.is_active AND i.market = 'US' "
        "  AND i.instrument_type IN ('stock','adr') "
        "  AND i.sector_gics = :sec AND i.id <> :self"),
        {"on": as_of, "sec": sector, "self": instrument_id}).all()
    n_peers = 0
    for (payload,) in rows:
        if not isinstance(payload, dict):
            continue
        used = False
        for key, path in _MULTIPLES:
            v = _num(_get(payload, path))
            if v is not None and math.isfinite(v) and v > 0:
                out[key].append(v)
                used = True
        if used:
            n_peers += 1
    return out, n_peers


def _percentile(value: float, population: list[float]) -> float | None:
    """Fraction of the population <= value (0..1). Higher percentile on a
    valuation multiple => richer than more of the sector."""
    if not population:
        return None
    return sum(1 for p in population if p <= value) / len(population)


def _comparables(session: Session, instrument_id: str, sector: str | None,
                 payload: dict[str, object], as_of: date, *,
                 eps_ttm: float | None, ebitda_ttm: float | None,
                 revenue_ttm: float | None, book_ps: float | None,
                 net_debt: float | None, shares: float | None,
                 price: float | None) -> dict[str, object]:
    peers, n_peers = _peer_multiples(session, instrument_id, sector, as_of)
    # stock's own metric per multiple, and the per-share value the peer median
    # implies for it.
    implied: list[float] = []
    detail: dict[str, object] = {}
    for key, path in _MULTIPLES:
        pop = peers[key]
        med = statistics.median(pop) if pop else None
        own = _num(_get(payload, path))
        pctile = _percentile(own, pop) if own is not None else None
        imp: float | None = None
        if med is not None and shares and shares > 0:
            if key == "pe" and eps_ttm is not None:
                imp = med * eps_ttm
            elif key == "ev_ebitda" and ebitda_ttm is not None and net_debt is not None:
                imp = (med * ebitda_ttm - net_debt) / shares
            elif key == "ps" and revenue_ttm is not None:
                imp = med * revenue_ttm / shares
            elif key == "pb" and book_ps is not None:
                imp = med * book_ps
        # only a POSITIVE implied value is a meaningful comparable — a negative
        # EPS / book / EBITDA would otherwise yield a sub-zero fair value and
        # pollute the blend (the same rule the peer filter applies).
        if imp is not None and math.isfinite(imp) and imp > 0:
            implied.append(imp)
        detail[key] = {"stock": own, "peer_median": med, "n_peers": len(pop),
                       "percentile": pctile, "implied_value": imp}
    blended = statistics.median(implied) if implied else None
    return {
        "sector": sector, "n_peers": n_peers, "multiples": detail,
        "blended_fair_value": blended,
        "upside_pct": (blended / price - 1.0) if (blended and price) else None,
    }


# ---------------------------------------------------------------------------
# DuPont ROE decomposition
# ---------------------------------------------------------------------------

def _dupont(payload: dict[str, object], as_of: date) -> dict[str, object]:
    ni = _latest(payload, "Income_Statement", "netIncome", as_of)
    rev = _latest(payload, "Income_Statement", "totalRevenue", as_of)
    assets = _latest(payload, "Balance_Sheet", "totalAssets", as_of)
    equity = _latest(payload, "Balance_Sheet", "totalStockholderEquity", as_of)
    net_margin = (ni / rev) if (ni is not None and rev and rev > 0) else None
    asset_turnover = (rev / assets) if (rev is not None and assets and assets > 0) else None
    equity_mult = (assets / equity) if (assets is not None and equity and equity > 0) else None
    roe = (net_margin * asset_turnover * equity_mult
           if net_margin is not None and asset_turnover is not None
           and equity_mult is not None else None)
    return {"net_margin": net_margin, "asset_turnover": asset_turnover,
            "equity_multiplier": equity_mult, "roe": roe}


# ---------------------------------------------------------------------------
# Top level
# ---------------------------------------------------------------------------

def compute_valuation(session: Session, instrument_id: str, symbol: str,
                      as_of: date) -> dict[str, object]:
    """Atlas's mechanical valuation panel for one stock at `as_of` (see module
    docstring). Every section is present and shaped consistently; values are
    None where inputs are insufficient (fail-soft, never fabricated)."""
    row = session.execute(text(
        "SELECT as_of, payload FROM market.fundamentals WHERE instrument_id = :i "
        "  AND as_of <= :on ORDER BY as_of DESC LIMIT 1"),
        {"i": instrument_id, "on": as_of}).first()
    payload = row.payload if (row is not None and isinstance(row.payload, dict)) else {}
    snapshot_as_of = row.as_of.isoformat() if row is not None else None

    price = _latest_close(session, instrument_id, as_of)
    shares = _shares(payload)
    net_debt = _net_debt(payload, as_of)
    tax = _effective_tax(payload, as_of)
    revenue_ttm = _num(_get(payload, ("Highlights", "RevenueTTM")))
    ebitda_ttm = _num(_get(payload, ("Highlights", "EBITDA")))
    eps_ttm = _num(_get(payload, ("Highlights", "EarningsShare")))
    book_ps = _num(_get(payload, ("Highlights", "BookValue")))
    sector = session.execute(text(
        "SELECT sector_gics FROM market.instruments WHERE id = :i"),
        {"i": instrument_id}).scalar()
    market_cap = (price * shares) if (price and shares) else None

    coc = _cost_of_capital(payload, as_of, market_cap=market_cap, tax=tax)
    wacc_v = coc["wacc"]
    wacc: float | None = wacc_v if isinstance(wacc_v, (int, float)) else None
    epv = _epv(payload, as_of, revenue_ttm=revenue_ttm, net_debt=net_debt,
               shares=shares, wacc=wacc, tax=tax, price=price)
    dcf = _dcf(payload, as_of, net_debt=net_debt, shares=shares, wacc=wacc,
               tax=tax, price=price)
    comps = _comparables(session, instrument_id, sector, payload, as_of,
                         eps_ttm=eps_ttm, ebitda_ttm=ebitda_ttm,
                         revenue_ttm=revenue_ttm, book_ps=book_ps,
                         net_debt=net_debt, shares=shares, price=price)
    dupont = _dupont(payload, as_of)

    # fair-value range across methods (each method's central estimate)
    centrals: list[tuple[str, float]] = []
    if isinstance(epv["fair_value_per_share"], (int, float)):
        centrals.append(("EPV (no-growth floor)", float(epv["fair_value_per_share"])))
    if isinstance(dcf["fair_value_per_share"], (int, float)):
        centrals.append(("DCF (central)", float(dcf["fair_value_per_share"])))
    if isinstance(comps["blended_fair_value"], (int, float)):
        centrals.append(("Comparables (blended)", float(comps["blended_fair_value"])))
    fv_values = [v for _n, v in centrals]
    summary: dict[str, object] = {
        "price": price,
        "methods": [n for n, _v in centrals],
        "fair_value_low": min(fv_values) if fv_values else None,
        "fair_value_central": statistics.median(fv_values) if fv_values else None,
        "fair_value_high": max(fv_values) if fv_values else None,
        "verdict": None, "upside_to_central_pct": None,
        "note": ("Atlas mechanical models — educational, assumption-sensitive, "
                 "NOT a price target. These methods do not credit hyper-growth; "
                 "a rich verdict on a high-growth name reflects that by design."),
    }
    if fv_values and price:
        lo = min(fv_values)
        hi = max(fv_values)
        central = statistics.median(fv_values)
        summary["upside_to_central_pct"] = central / price - 1.0
        summary["verdict"] = ("below our model range" if price < lo else
                              "above our model range" if price > hi else
                              "within our model range")

    return {
        "as_of": as_of.isoformat(), "snapshot_as_of": snapshot_as_of,
        "price": price, "shares_outstanding": shares, "net_debt": net_debt,
        "cost_of_capital": coc, "epv": epv, "dcf": dcf,
        "comparables": comps, "dupont": dupont, "summary": summary,
    }
