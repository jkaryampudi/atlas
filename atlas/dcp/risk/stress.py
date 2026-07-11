"""Stress testing (Doc 04 §7): scenario library v1, pure scenario math over
holdings, and the marginal broad-equity-crash policy gate.

Scenario math is DCP — factor shocks applied to holdings via betas/sector
mappings; the Stress Testing Agent only selects scenarios and writes the
plain-English summary (§7). Reported per scenario: pro-forma NAV impact,
distance-to-breaker (which DD level the loss would trigger, §5), and the worst
single-name contributors. Policy rule (§7): a proposal whose marginal effect
pushes the "broad equity crash" scenario loss beyond -25% of NAV fails risk —
`stress_marginal_gate` returns that verdict as a RuleResult to sit alongside
engine.validate's itemised L1-L11 output (validate itself is untouched).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from atlas.dcp.risk.engine import (
    _BROAD_SECTOR,
    BreakerLevel,
    RuleResult,
    TradeProposal,
    computed_breaker,
    drawdown,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_CENT = Decimal("0.01")
_PCT = Decimal("0.000001")

STRESS_CRASH_LOSS_LIMIT = Decimal("-0.25")  # §7 policy rule, strict ("beyond")


@dataclass(frozen=True)
class Scenario:
    """One row of the §7 scenario-library-v1 table. A scenario sets only its
    own shocks; the rest stay at their identity values, so one composition
    rule (multiplicative, per holding) serves the whole library."""
    key: str
    shock_definition: str                    # verbatim from the Doc 04 §7 table
    us_equity_shock: Decimal = _ZERO         # non-India equity move
    india_equity_shock: Decimal = _ZERO      # India sleeve incl. ADR/ETF look-through
    rate_shock_bp: Decimal = _ZERO           # US 10Y move, applied via holding betas
    india_fx_shock: Decimal = _ZERO          # INR vs USD, look-through translation
    largest_sector_shock: Decimal = _ZERO    # applied to the largest non-Broad sector
    aud_appreciation: Decimal = _ZERO        # AUD vs all non-AUD ccy (translation)
    spread_multiple: Decimal = _ONE          # informational execution-cost stress;
    fill_slippage: Decimal = _ZERO           # ...the NAV-impact carrier is slippage


# §7 scenario library v1, verbatim. "Correlations -> 1" needs no extra term:
# the book is long-only and per-holding losses are summed with no
# diversification offset, which IS the correlations-to-one assumption.
SCENARIO_LIBRARY_V1: tuple[Scenario, ...] = (
    Scenario(key="broad_equity_crash",
             shock_definition="US -20%, India -25%, correlations -> 1 assumption",
             us_equity_shock=Decimal("-0.20"),
             india_equity_shock=Decimal("-0.25")),
    Scenario(key="rates_shock",
             shock_definition="+150bp US 10Y; duration-sensitive sectors shocked "
                              "via beta table",
             rate_shock_bp=Decimal("150")),
    Scenario(key="india_shock",
             shock_definition="Nifty -15%, INR -8% vs USD",
             india_equity_shock=Decimal("-0.15"),
             india_fx_shock=Decimal("-0.08")),
    Scenario(key="sector_collapse",
             shock_definition="Largest portfolio sector -35%",
             largest_sector_shock=Decimal("-0.35")),
    Scenario(key="aud_spike",
             shock_definition="AUD +10% vs USD and INR (translation loss)",
             aud_appreciation=Decimal("0.10")),
    Scenario(key="liquidity_event",
             shock_definition="Spreads 5x, fills at -2% slippage assumption",
             spread_multiple=Decimal("5"),
             fill_slippage=Decimal("-0.02")),
)

SCENARIOS_BY_KEY: dict[str, Scenario] = {s.key: s for s in SCENARIO_LIBRARY_V1}


@dataclass(frozen=True)
class StressHolding:
    """Stress-relevant view of one holding (or of a proposal, pro-forma).
    `rate_beta_per_100bp` is the §7 beta-table input: equity move per +100bp on
    the US 10Y (negative for duration-sensitive sectors), supplied per holding
    by the caller — the beta table itself is data, not code."""
    symbol: str
    value_aud: Decimal
    sector_gics: str
    india_exposed: bool          # ADR/ETF look-through included, as in L4
    currency: str
    rate_beta_per_100bp: Decimal = _ZERO


@dataclass(frozen=True)
class Contribution:
    symbol: str
    loss_aud: Decimal            # quantized to cents


@dataclass(frozen=True)
class ScenarioResult:
    key: str
    loss_aud: Decimal                        # signed AUD, quantized to cents
    nav_impact_pct: Decimal                  # loss / NAV, quantized to 1e-6
    breaker_after: BreakerLevel              # DD level this loss would trigger (§5)
    worst_contributors: tuple[Contribution, ...]  # top 3, most negative first


def _largest_sector(holdings: Sequence[StressHolding]) -> str | None:
    """Largest non-Broad GICS sector by AUD value (engine L3 convention:
    diversified 'Broad' ETFs are not a sector bet). Ties break alphabetically
    for determinism; an all-Broad or empty book has no largest sector."""
    totals: dict[str, Decimal] = {}
    for h in holdings:
        if h.sector_gics != _BROAD_SECTOR:
            totals[h.sector_gics] = totals.get(h.sector_gics, _ZERO) + h.value_aud
    if not totals:
        return None
    return max(sorted(totals), key=lambda s: totals[s])


def _holding_loss(scenario: Scenario, holding: StressHolding,
                  largest_sector: str | None) -> Decimal:
    """Signed AUD P&L of one holding under one scenario. Shocks compound
    multiplicatively (the India scenario is equity x FX: 0.85 x 0.92 - 1);
    identity shocks leave the factor unchanged, so this single rule prices
    every library scenario."""
    factor = _ONE + (scenario.india_equity_shock if holding.india_exposed
                     else scenario.us_equity_shock)
    factor *= _ONE + holding.rate_beta_per_100bp * scenario.rate_shock_bp / 100
    if holding.india_exposed:
        factor *= _ONE + scenario.india_fx_shock
    if holding.sector_gics == largest_sector:
        factor *= _ONE + scenario.largest_sector_shock
    if holding.currency != "AUD":
        # AUD up x% -> non-AUD value translates at 1/(1+x). Denomination-ccy
        # proxy, matching L11: an AUD-listed India ETF's INR look-through
        # translation is carried by the india_fx_shock leg, not this one.
        factor /= _ONE + scenario.aud_appreciation
    factor *= _ONE + scenario.fill_slippage
    return holding.value_aud * (factor - _ONE)


def run_scenario(scenario: Scenario, holdings: Sequence[StressHolding], *,
                 nav_aud: Decimal, high_water_mark: Decimal) -> ScenarioResult:
    """Pure §7 scenario math. Distance-to-breaker is measured against the
    high-water mark, so an existing drawdown deepens the scenario verdict."""
    if nav_aud <= 0:
        raise ValueError("NAV must be positive")
    largest = _largest_sector(holdings)
    losses = [(h.symbol, _holding_loss(scenario, h, largest)) for h in holdings]
    total = sum((loss for _, loss in losses), _ZERO)
    breaker_after = computed_breaker(drawdown(nav_aud + total, high_water_mark))
    worst = sorted(((s, x) for s, x in losses if x < 0),
                   key=lambda t: (t[1], t[0]))[:3]
    return ScenarioResult(
        key=scenario.key,
        loss_aud=total.quantize(_CENT),
        nav_impact_pct=(total / nav_aud).quantize(_PCT),
        breaker_after=breaker_after,
        worst_contributors=tuple(Contribution(s, x.quantize(_CENT))
                                 for s, x in worst))


def run_library(holdings: Sequence[StressHolding], *, nav_aud: Decimal,
                high_water_mark: Decimal,
                library: Sequence[Scenario] = SCENARIO_LIBRARY_V1,
                ) -> tuple[ScenarioResult, ...]:
    """Weekly run and per-proposal-batch run (§7) share this entry point."""
    return tuple(run_scenario(s, holdings, nav_aud=nav_aud,
                              high_water_mark=high_water_mark) for s in library)


def stress_marginal_gate(proposal: TradeProposal,
                         holdings: Sequence[StressHolding], *,
                         nav_aud: Decimal) -> RuleResult:
    """§7 policy rule, evaluated standalone alongside engine.validate: the
    with-proposal (pro-forma) book's broad-equity-crash loss must not go
    beyond -25% of NAV (strict — exactly -25% passes). NAV is unchanged by
    the purchase itself (cash becomes holdings), and the detail reports the
    without-proposal loss too, so the marginal effect is auditable."""
    proposed = StressHolding(symbol=proposal.symbol, value_aud=proposal.cost_aud,
                             sector_gics=proposal.sector_gics,
                             india_exposed=proposal.india_exposed,
                             currency=proposal.currency)
    crash = SCENARIOS_BY_KEY["broad_equity_crash"]
    base = run_scenario(crash, tuple(holdings), nav_aud=nav_aud,
                        high_water_mark=nav_aud)
    pro_forma = run_scenario(crash, (*tuple(holdings), proposed),
                             nav_aud=nav_aud, high_water_mark=nav_aud)
    return RuleResult(
        "STRESS", pro_forma.nav_impact_pct >= STRESS_CRASH_LOSS_LIMIT,
        f"broad-equity-crash pro-forma {pro_forma.nav_impact_pct:.4f} "
        f"(without proposal {base.nav_impact_pct:.4f}) vs limit "
        f"{STRESS_CRASH_LOSS_LIMIT}")
