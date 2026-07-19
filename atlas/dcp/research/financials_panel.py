"""Per-stock FINANCIALS PANEL for the research dossier — the reported financial
statements, earnings history, forward consensus, and the extra "key indicator"
facts a pro-research report shows, surfaced from data Atlas ALREADY STORES:

  * full statements (income / balance-sheet / cash-flow, annual + quarterly) —
    read from the stored EODHD fundamentals payload (`market.fundamentals`),
  * completed-quarter earnings surprises — from `market.earnings_surprises`
    (immutable facts ingested by earnings_history.py),
  * forward consensus + EPS revisions — the latest per-period snapshot from
    `market.estimate_snapshots` (the ADR-0011 append-only archive),
  * key stats not already in the model panel — vendor 5-year beta, book value
    per share, revenue (ttm), shares outstanding, FCF and a derived FCF yield,
    forward-year EPS/revenue consensus, and the Wall-Street target.

NOTHING HERE IS FABRICATED (Constitution: no invented numbers). Every value is
a vendor-reported fact rendered verbatim, or a clearly-labelled mechanical ratio
of two vendor facts (FCF yield). Fields are None where the vendor did not send
them — honest absence, never a guess.

POINT-IN-TIME. The panel is bounded to `as_of`: the fundamentals snapshot is the
latest with `as_of <= as_of`; statement periods are filtered to those whose
period-end date is `<= as_of`; earnings surprises to those whose ANNOUNCEMENT
(`report_date`) is `<= as_of` (a report is not public before it is announced);
estimate snapshots to those recorded on a session `<= as_of`. So the dossier
shows what was knowable at the pick's as-of session, not hindsight.

NUMERIC-ONLY CHOKE (defence in depth). Although this panel feeds the human
dossier and not an agent prompt, every payload read goes through the same
`_number` choke used for agent evidence (atlas/dcp/market_data/fundamentals.py):
free text, bools, and NaN/inf never render. Only numbers and ISO dates leave
this module.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.dcp.market_data.fundamentals import _currency, _get, _number

# Curated line items per statement (label -> vendor key), in render order. A
# pro-research report shows the headline lines, not the vendor's full ~40-field
# dump; this is the readable subset, matching the report's statement pages.
_INCOME_ITEMS: tuple[tuple[str, str], ...] = (
    ("Revenue", "totalRevenue"),
    ("Gross Profit", "grossProfit"),
    ("Operating Income", "operatingIncome"),
    ("EBITDA", "ebitda"),
    ("R&D", "researchDevelopment"),
    ("SG&A", "sellingGeneralAdministrative"),
    ("Net Income", "netIncome"),
)
_BALANCE_ITEMS: tuple[tuple[str, str], ...] = (
    ("Total Current Assets", "totalCurrentAssets"),
    ("Total Assets", "totalAssets"),
    ("Total Current Liabilities", "totalCurrentLiabilities"),
    ("Total Liabilities", "totalLiab"),
    ("Total Equity", "totalStockholderEquity"),
    ("Total Debt", "shortLongTermDebtTotal"),
    ("Cash & ST Investments", "cashAndShortTermInvestments"),
)
_CASHFLOW_ITEMS: tuple[tuple[str, str], ...] = (
    ("Operating Cash Flow", "totalCashFromOperatingActivities"),
    ("Capital Expenditures", "capitalExpenditures"),
    ("Free Cash Flow", "freeCashFlow"),
    ("Investing Cash Flow", "totalCashflowsFromInvestingActivities"),
    ("Financing Cash Flow", "totalCashFromFinancingActivities"),
)

# How many periods of each granularity to surface (report shows ~5 / ~5).
_N_YEARLY = 5
_N_QUARTERLY = 6
# Completed-quarter surprise history depth (report shows ~9 rows).
_N_SURPRISES = 12


def _num(value: object) -> float | None:
    """Vendor value -> float through the numeric choke, or None. Accepts the
    string decimals EODHD stores ('64462000000.00') and real numbers alike."""
    rendered = _number(value)
    if rendered is None:
        return None
    try:
        return float(rendered)
    except ValueError:
        return None


def _period_date(row: object) -> date | None:
    """A statement row's period-end date from its `date` field, or None if the
    row is malformed / the date does not parse (dropped, never guessed)."""
    if not isinstance(row, dict):
        return None
    try:
        return date.fromisoformat(str(row.get("date")))
    except (TypeError, ValueError):
        return None


def _statement_rows(section: object, items: tuple[tuple[str, str], ...],
                    as_of: date, limit: int) -> list[dict[str, object]]:
    """The last `limit` periods of one statement granularity (the vendor's
    date-keyed dict), point-in-time bounded to period-end `<= as_of`, oldest
    first. Each row is {period, values{label: number|None}}; a period with no
    readable line item at all is dropped (nothing to show)."""
    if not isinstance(section, dict):
        return []
    dated: list[tuple[date, dict[str, object]]] = []
    for _key, row in section.items():
        pend = _period_date(row)
        if pend is None or pend > as_of or not isinstance(row, dict):
            continue
        values = {label: _num(row.get(vkey)) for label, vkey in items}
        if all(v is None for v in values.values()):
            continue
        dated.append((pend, {"period": pend.isoformat(), "values": values}))
    dated.sort(key=lambda t: t[0])
    return [r for _d, r in dated[-limit:]]


def _statements(payload: dict[str, object], as_of: date) -> dict[str, object]:
    fin = payload.get("Financials")
    fin = fin if isinstance(fin, dict) else {}

    def block(section_key: str, items: tuple[tuple[str, str], ...]) -> dict[str, object]:
        section = fin.get(section_key)
        section = section if isinstance(section, dict) else {}
        return {
            "annual": _statement_rows(section.get("yearly"), items, as_of, _N_YEARLY),
            "quarterly": _statement_rows(section.get("quarterly"), items, as_of, _N_QUARTERLY),
        }

    return {
        "income": block("Income_Statement", _INCOME_ITEMS),
        "balance": block("Balance_Sheet", _BALANCE_ITEMS),
        "cash_flow": block("Cash_Flow", _CASHFLOW_ITEMS),
    }


def _earnings_history(session: Session, instrument_id: str,
                     as_of: date) -> list[dict[str, object]]:
    """Completed-quarter EPS surprises announced on or before `as_of`, most
    recent first — settled facts from market.earnings_surprises."""
    rows = session.execute(text(
        "SELECT fiscal_period_end, report_date, eps_actual, eps_estimate, "
        "       surprise_pct FROM market.earnings_surprises "
        "WHERE instrument_id = :iid AND report_date <= :on "
        "ORDER BY fiscal_period_end DESC LIMIT :n"),
        {"iid": instrument_id, "on": as_of, "n": _N_SURPRISES}).all()
    out: list[dict[str, object]] = []
    for r in rows:
        out.append({
            "fiscal_period_end": r.fiscal_period_end.isoformat(),
            "report_date": r.report_date.isoformat(),
            "eps_actual": float(r.eps_actual) if r.eps_actual is not None else None,
            "eps_estimate": float(r.eps_estimate) if r.eps_estimate is not None else None,
            "surprise_pct": float(r.surprise_pct) if r.surprise_pct is not None else None,
        })
    return out


def _forward_estimates(session: Session, instrument_id: str,
                      as_of: date) -> list[dict[str, object]]:
    """Forward consensus per fiscal period: the LATEST snapshot recorded on or
    before `as_of` for each still-forward period (fiscal_period_end >= as_of),
    nearest period first. Reads the ADR-0011 archive (market.estimate_snapshots).
    Young archive => few/no rows; that is honest, not an error."""
    rows = session.execute(text(
        "SELECT DISTINCT ON (fiscal_period_end) fiscal_period_end, snapshot_date, "
        "       eps_estimate_avg, eps_estimate_analysts, revenue_estimate_avg, "
        "       eps_trend_current, eps_trend_30d, revisions_up_30d, revisions_down_30d "
        "FROM market.estimate_snapshots "
        "WHERE instrument_id = :iid AND snapshot_date <= :on "
        "  AND fiscal_period_end >= :on "
        "ORDER BY fiscal_period_end, snapshot_date DESC"),
        {"iid": instrument_id, "on": as_of}).all()

    def f(v: object) -> float | None:
        return float(v) if isinstance(v, (int, float, Decimal)) else None

    out: list[dict[str, object]] = []
    for r in rows:
        out.append({
            "fiscal_period_end": r.fiscal_period_end.isoformat(),
            "snapshot_date": r.snapshot_date.isoformat(),
            "eps_estimate_avg": f(r.eps_estimate_avg),
            "eps_estimate_analysts": f(r.eps_estimate_analysts),
            "revenue_estimate_avg": f(r.revenue_estimate_avg),
            "eps_trend_current": f(r.eps_trend_current),
            "eps_trend_30d": f(r.eps_trend_30d),
            "revisions_up_30d": f(r.revisions_up_30d),
            "revisions_down_30d": f(r.revisions_down_30d),
        })
    return out


def _key_stats(payload: dict[str, object]) -> dict[str, object]:
    """The report's extra "Key Indicators" not already in the model panel — all
    vendor facts, plus a single clearly-derived ratio (FCF yield)."""
    beta = _num(_get(payload, ("Technicals", "Beta")))
    book_ps = _num(_get(payload, ("Highlights", "BookValue")))
    revenue_ttm = _num(_get(payload, ("Highlights", "RevenueTTM")))
    market_cap = _num(_get(payload, ("Highlights", "MarketCapitalization")))
    shares_out = _num(_get(payload, ("SharesStats", "SharesOutstanding")))
    eps_est_cy = _num(_get(payload, ("Highlights", "EPSEstimateCurrentYear")))
    eps_est_ny = _num(_get(payload, ("Highlights", "EPSEstimateNextYear")))
    ws_target = _num(_get(payload, ("Highlights", "WallStreetTargetPrice")))
    mrq = _get(payload, ("Highlights", "MostRecentQuarter"))

    # FCF (ttm) as the latest yearly free cash flow, then a derived FCF yield.
    fcf_ttm: float | None = None
    yearly = _get(payload, ("Financials", "Cash_Flow", "yearly"))
    if isinstance(yearly, dict):
        latest: date | None = None
        for _k, row in yearly.items():
            pend = _period_date(row)
            if pend is not None and (latest is None or pend > latest) \
                    and isinstance(row, dict):
                cand = _num(row.get("freeCashFlow"))
                if cand is not None:
                    latest, fcf_ttm = pend, cand
    fcf_yield = (100.0 * fcf_ttm / market_cap
                 if (fcf_ttm is not None and market_cap) else None)

    return {
        "beta_5y": beta,
        "book_value_per_share": book_ps,
        "revenue_ttm": revenue_ttm,
        "market_cap": market_cap,
        "shares_outstanding": shares_out,
        "fcf": fcf_ttm,
        "fcf_yield_pct": fcf_yield,           # derived: FCF / market cap
        "eps_estimate_current_year": eps_est_cy,
        "eps_estimate_next_year": eps_est_ny,
        "wall_street_target": ws_target,
        "most_recent_quarter": (mrq if isinstance(mrq, str) else None),
    }


def compute_financials(session: Session, instrument_id: str, symbol: str,
                       as_of: date) -> dict[str, object]:
    """The full financials panel for one stock at `as_of` (see module docstring).
    Every section is present and shaped consistently; sections are empty / None
    where the underlying data is absent (fail-soft, never fabricated)."""
    row = session.execute(text(
        "SELECT as_of, payload FROM market.fundamentals WHERE instrument_id = :i "
        "  AND as_of <= :on ORDER BY as_of DESC LIMIT 1"),
        {"i": instrument_id, "on": as_of}).first()
    payload = row.payload if (row is not None and isinstance(row.payload, dict)) else {}
    snapshot_as_of = row.as_of.isoformat() if row is not None else None
    # currency through the same ISO-4217 shape guard as agent evidence — honours
    # the module's "only numbers and ISO dates leave here" contract (a hostile
    # General.CurrencyCode free-text string is dropped, not displayed).
    currency = _currency(payload)

    return {
        "as_of": as_of.isoformat(),
        "snapshot_as_of": snapshot_as_of,
        "currency": currency,
        "statements": _statements(payload, as_of),
        "earnings": {
            "history": _earnings_history(session, instrument_id, as_of),
            "estimates": _forward_estimates(session, instrument_id, as_of),
        },
        "key_stats": _key_stats(payload),
    }
