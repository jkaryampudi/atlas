"""Risk Engine (Doc 04, implemented verbatim): L1-L11 hard limits, deterministic
position sizing (§4), drawdown circuit breakers (§5), worst-case pro-forma math.

Risk is a structural property, not an opinion. `validate` returns an ITEMISED
RiskCheck — every rule is evaluated (no short-circuit) so a FAIL explains itself
completely. A FAIL is terminal for the proposal (Constitution 3.2): there is no
override path in code, and agents hold no write permission on risk.* tables.
Position size is an OUTPUT of risk, never an input from conviction.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

MIN_POSITION_AUD = Decimal("2000")  # §4 minimum economic position
_BROAD_SECTOR = "Broad"             # diversified ETFs are not a GICS sector bet (L3)


# ---------------------------------------------------------------- breakers §5

class BreakerLevel(StrEnum):
    NONE = "none"
    DD1 = "DD1"
    DD2 = "DD2"
    DD3 = "DD3"


_SEVERITY = {BreakerLevel.NONE: 0, BreakerLevel.DD1: 1,
             BreakerLevel.DD2: 2, BreakerLevel.DD3: 3}

DD1_TRIGGER = Decimal("-0.05")
DD2_TRIGGER = Decimal("-0.10")
DD3_TRIGGER = Decimal("-0.15")


def drawdown(nav_aud: Decimal, high_water_mark: Decimal) -> Decimal:
    if high_water_mark <= 0:
        raise ValueError("high-water mark must be positive")
    return (nav_aud - high_water_mark) / high_water_mark


def computed_breaker(dd: Decimal) -> BreakerLevel:
    if dd <= DD3_TRIGGER:
        return BreakerLevel.DD3
    if dd <= DD2_TRIGGER:
        return BreakerLevel.DD2
    if dd <= DD1_TRIGGER:
        return BreakerLevel.DD1
    return BreakerLevel.NONE


def next_breaker_state(current: BreakerLevel, dd: Decimal, *,
                       human_cleared: bool = False) -> BreakerLevel:
    """DD2/DD3 LATCH: they can only step down through the dual-confirmed human
    action (§5); agents cannot clear them. DD1 tracks the drawdown directly."""
    target = computed_breaker(dd)
    if current in (BreakerLevel.DD2, BreakerLevel.DD3) and not human_cleared:
        return target if _SEVERITY[target] >= _SEVERITY[current] else current
    return target


# ------------------------------------------------------------------ limits §3

@dataclass(frozen=True)
class Limits:
    version: int
    l1_max_stock_weight: Decimal
    l2_max_etf_weight: Decimal
    l3_max_sector_exposure: Decimal
    l4_max_india_sleeve: Decimal
    l5_min_cash_reserve: Decimal
    l6_max_risk_per_trade: Decimal
    l7_max_aggregate_open_risk: Decimal
    l8_corr_threshold: Decimal
    l8_corr_combined_weight: Decimal
    l9_max_new_positions_per_day: int
    l10_max_pct_adv: Decimal
    l11_max_non_aud_exposure: Decimal

    def risk_per_trade(self, breaker: BreakerLevel) -> Decimal:
        """DD1 halves new-position risk (L6 -> 0.5%); §5."""
        if breaker is BreakerLevel.DD1:
            return self.l6_max_risk_per_trade / 2
        return self.l6_max_risk_per_trade


def limits_from_json(version: int, doc: dict[str, object]) -> Limits:
    d = {k: Decimal(str(v)) for k, v in doc.items()}
    return Limits(
        version=version,
        l1_max_stock_weight=d["L1_max_stock_weight"],
        l2_max_etf_weight=d["L2_max_etf_weight"],
        l3_max_sector_exposure=d["L3_max_sector_exposure"],
        l4_max_india_sleeve=d["L4_max_india_sleeve"],
        l5_min_cash_reserve=d["L5_min_cash_reserve"],
        l6_max_risk_per_trade=d["L6_max_risk_per_trade"],
        l7_max_aggregate_open_risk=d["L7_max_aggregate_open_risk"],
        l8_corr_threshold=d["L8_corr_threshold"],
        l8_corr_combined_weight=d["L8_corr_combined_weight"],
        l9_max_new_positions_per_day=int(d["L9_max_new_positions_per_day"]),
        l10_max_pct_adv=d["L10_max_pct_adv"],
        l11_max_non_aud_exposure=d["L11_max_non_aud_exposure"],
    )


def load_active_limit_set(session: Session, on: date) -> Limits:
    row = session.execute(text(
        "SELECT version, limits FROM risk.limit_sets "
        "WHERE effective_from <= :on ORDER BY version DESC LIMIT 1"),
        {"on": on}).first()
    if row is None:
        raise RuntimeError(f"no limit set effective on {on} — risk cannot run")
    limits = row.limits if isinstance(row.limits, dict) else json.loads(row.limits)
    return limits_from_json(int(row.version), limits)


# --------------------------------------------------------- pro-forma inputs §3

@dataclass(frozen=True)
class HoldingRisk:
    """Risk-relevant view of one existing holding or pending unfilled order leg."""
    symbol: str
    value_aud: Decimal
    sector_gics: str
    india_exposed: bool          # ADR/ETF look-through included (L4)
    currency: str
    risk_to_stop_aud: Decimal | None  # stop-out loss (L7); None => NO stop-out
    #   distance (no stop). The engine, not the book-builder, then decides the
    #   L7 contribution: a satellite fails closed to its full value, a core
    #   holding contributes zero — see _holding_open_risk. Never conflate the
    #   two: a satellite whose stop was dropped by a bug MUST NOT read as zero.
    is_core: bool = False        # ADR-0014 positive core marker: a legitimately
    #   stopless passive-index holding (rebalanced, not stopped). Its market
    #   exposure is captured by the weight rules (L1-L5/L11), so it contributes
    #   ZERO stop-out open risk to L7. Defaults false, so any holding NOT
    #   explicitly marked core is treated as a satellite that must be stopped.


def _holding_open_risk(h: HoldingRisk) -> Decimal:
    """ADR-0014 L7 open-risk contribution of ONE existing holding, stop-based
    and core-aware. Three mutually exclusive cases, in safety order:

    1. is_core: a passive-index core holding is rebalanced, not stopped — its
       exposure is a weight-rule matter (L1-L5/L11), never a stop-out risk. It
       contributes ZERO. This branch is reached ONLY through the explicit,
       positive core marker (migration 0023's trading.positions.is_core).
    2. NOT core AND no stop-out distance (risk_to_stop_aud is None): a satellite
       must be stop-protected; a missing stop is a bug or a data error, so it
       FAILS CLOSED and counts its FULL value as open risk — exactly the
       pre-ADR-0014 behavior. Never inferred to be zero.
    3. NOT core AND a stop: the stop-out loss the book-builder computed
       (max(0, ref - stop) * qty * fx).
    """
    if h.is_core:
        return Decimal(0)
    if h.risk_to_stop_aud is None:        # satellite missing a stop: fail closed
        return h.value_aud
    return h.risk_to_stop_aud


@dataclass(frozen=True)
class TradeProposal:
    symbol: str
    side: Literal["BUY"]         # long-only mandate; exits release risk, not add it
    qty: int
    entry_price: Decimal         # local currency
    stop_price: Decimal
    fx_to_aud: Decimal           # AUD per 1 unit of local currency
    instrument_type: Literal["stock", "etf", "adr"]
    sector_gics: str
    india_exposed: bool
    currency: str
    adv_20d: int                 # 20-day average daily volume, shares (L10)
    corr_with_existing: dict[str, Decimal]  # existing symbol -> 90d correlation (L8)

    @property
    def cost_aud(self) -> Decimal:
        return Decimal(self.qty) * self.entry_price * self.fx_to_aud

    @property
    def risk_aud(self) -> Decimal:
        return Decimal(self.qty) * (self.entry_price - self.stop_price) * self.fx_to_aud


@dataclass(frozen=True)
class PortfolioState:
    """Worst-case base state: existing holdings PLUS approved-but-unfilled
    pending orders, as if they all execute (§3 'worst-case pro-forma')."""
    nav_aud: Decimal
    cash_aud: Decimal            # already net of pending order costs
    holdings: tuple[HoldingRisk, ...]
    new_positions_today: int


@dataclass(frozen=True)
class RuleResult:
    """Doc 05 §4: results are itemised as {rule, value, limit, pass, detail}.
    value/limit stay None for qualitative rules (DD gate, n/a branches)."""
    rule: str
    passed: bool
    detail: str
    value: Decimal | None = None
    limit: Decimal | None = None


@dataclass(frozen=True)
class RiskCheck:
    passed: bool
    breaker: BreakerLevel
    results: tuple[RuleResult, ...]

    def failures(self) -> tuple[RuleResult, ...]:
        return tuple(r for r in self.results if not r.passed)


def _weight(value: Decimal, nav: Decimal) -> Decimal:
    return value / nav


# ------------------------------------------------------------------ validate §2

def validate(proposal: TradeProposal, state: PortfolioState, limits: Limits,
             breaker: BreakerLevel = BreakerLevel.NONE) -> RiskCheck:
    """Evaluate every rule against the post-trade worst-case pro-forma.
    All rules are always evaluated — a FAIL must explain itself completely."""
    nav = state.nav_aud
    if nav <= 0:
        raise ValueError("NAV must be positive")
    results: list[RuleResult] = []

    # Breaker gate (§5): DD2 = no new positions; DD3 = exit-only full halt.
    is_new_position = all(h.symbol != proposal.symbol for h in state.holdings)
    if breaker in (BreakerLevel.DD2, BreakerLevel.DD3):
        results.append(RuleResult("DD", False,
                                  f"breaker {breaker.value}: no new positions "
                                  f"({'exit-only' if breaker is BreakerLevel.DD3 else 'human review required'})"))
    else:
        results.append(RuleResult("DD", True, f"breaker {breaker.value}"))

    # post-trade candidate weight (existing weight in the same symbol counts)
    existing_symbol_value = sum((h.value_aud for h in state.holdings
                                 if h.symbol == proposal.symbol), Decimal(0))
    cand_weight = _weight(existing_symbol_value + proposal.cost_aud, nav)

    if proposal.instrument_type == "etf":
        cap = limits.l2_max_etf_weight
        results.append(RuleResult("L1", True, "n/a (ETF -> L2)"))
        results.append(RuleResult("L2", cand_weight <= cap,
                                  f"etf weight {cand_weight:.4f} vs cap {cap}",
                                  value=cand_weight, limit=cap))
    else:
        cap = limits.l1_max_stock_weight
        results.append(RuleResult("L1", cand_weight <= cap,
                                  f"stock weight {cand_weight:.4f} vs cap {cap}",
                                  value=cand_weight, limit=cap))
        results.append(RuleResult("L2", True, "n/a (stock/ADR -> L1)"))

    # L3 sector exposure (diversified 'Broad' ETFs are not a sector bet)
    if proposal.sector_gics != _BROAD_SECTOR:
        sector_value = sum((h.value_aud for h in state.holdings
                            if h.sector_gics == proposal.sector_gics), Decimal(0))
        sector_after = _weight(sector_value + proposal.cost_aud, nav)
        results.append(RuleResult("L3", sector_after <= limits.l3_max_sector_exposure,
                                  f"{proposal.sector_gics} {sector_after:.4f} vs cap "
                                  f"{limits.l3_max_sector_exposure}",
                                  value=sector_after,
                                  limit=limits.l3_max_sector_exposure))
    else:
        results.append(RuleResult("L3", True, "n/a (Broad ETF)"))

    # L4 India sleeve incl. look-through
    india_value = sum((h.value_aud for h in state.holdings if h.india_exposed),
                      Decimal(0))
    india_after = _weight(india_value + (proposal.cost_aud if proposal.india_exposed
                                         else Decimal(0)), nav)
    results.append(RuleResult("L4", india_after <= limits.l4_max_india_sleeve,
                              f"india sleeve {india_after:.4f} vs cap "
                              f"{limits.l4_max_india_sleeve}",
                              value=india_after, limit=limits.l4_max_india_sleeve))

    # L5 cash floor
    cash_after = _weight(state.cash_aud - proposal.cost_aud, nav)
    results.append(RuleResult("L5", cash_after >= limits.l5_min_cash_reserve,
                              f"cash after {cash_after:.4f} vs floor "
                              f"{limits.l5_min_cash_reserve}",
                              value=cash_after, limit=limits.l5_min_cash_reserve))

    # L6 risk per trade (DD1 halves the budget)
    l6_cap = limits.risk_per_trade(breaker)
    trade_risk_pct = _weight(proposal.risk_aud, nav)
    results.append(RuleResult("L6", trade_risk_pct <= l6_cap,
                              f"trade risk {trade_risk_pct:.4f} vs cap {l6_cap}",
                              value=trade_risk_pct, limit=l6_cap))

    # L7 aggregate open risk (ADR-0014: stop-based + core-aware per holding)
    open_risk = sum((_holding_open_risk(h) for h in state.holdings), Decimal(0))
    agg_after = _weight(open_risk + proposal.risk_aud, nav)
    results.append(RuleResult("L7", agg_after <= limits.l7_max_aggregate_open_risk,
                              f"aggregate open risk {agg_after:.4f} vs cap "
                              f"{limits.l7_max_aggregate_open_risk}",
                              value=agg_after,
                              limit=limits.l7_max_aggregate_open_risk))

    # L8 pairwise correlation concentration
    l8_pass, l8_detail = True, "no correlated concentration"
    l8_value: Decimal | None = None
    for h in state.holdings:
        corr = proposal.corr_with_existing.get(h.symbol)
        if corr is None or corr <= limits.l8_corr_threshold:
            continue
        combined = _weight(h.value_aud, nav) + cand_weight
        if combined > limits.l8_corr_combined_weight:
            l8_pass = False
            l8_value = combined
            l8_detail = (f"corr {corr} with {h.symbol} and combined weight "
                         f"{combined:.4f} > {limits.l8_corr_combined_weight}")
            break
    results.append(RuleResult("L8", l8_pass, l8_detail, value=l8_value,
                              limit=limits.l8_corr_combined_weight))

    # L9 new positions per day
    if is_new_position:
        l9_ok = state.new_positions_today + 1 <= limits.l9_max_new_positions_per_day
        results.append(RuleResult(
            "L9", l9_ok, f"{state.new_positions_today} opened today, cap "
                         f"{limits.l9_max_new_positions_per_day}",
            value=Decimal(state.new_positions_today + 1),
            limit=Decimal(limits.l9_max_new_positions_per_day)))
    else:
        results.append(RuleResult("L9", True, "n/a (existing position)"))

    # L10 liquidity — unknown ADV fails closed
    if proposal.adv_20d <= 0:
        results.append(RuleResult("L10", False, "no ADV data — fail closed"))
    else:
        adv_cap = limits.l10_max_pct_adv * Decimal(proposal.adv_20d)
        results.append(RuleResult("L10", Decimal(proposal.qty) <= adv_cap,
                                  f"qty {proposal.qty} vs {limits.l10_max_pct_adv} "
                                  f"of ADV {proposal.adv_20d}",
                                  value=Decimal(proposal.qty), limit=adv_cap))

    # L11 unhedged FX exposure
    non_aud = sum((h.value_aud for h in state.holdings if h.currency != "AUD"),
                  Decimal(0))
    non_aud_after = _weight(non_aud + (proposal.cost_aud
                                       if proposal.currency != "AUD" else Decimal(0)),
                            nav)
    results.append(RuleResult("L11", non_aud_after <= limits.l11_max_non_aud_exposure,
                              f"non-AUD {non_aud_after:.4f} vs cap "
                              f"{limits.l11_max_non_aud_exposure}",
                              value=non_aud_after,
                              limit=limits.l11_max_non_aud_exposure))

    return RiskCheck(passed=all(r.passed for r in results), breaker=breaker,
                     results=tuple(results))


# ------------------------------------------------------------------- sizing §4

@dataclass(frozen=True)
class SizeDecision:
    qty: int
    accepted: bool
    binding_constraint: str
    detail: str


def size_position(*, nav_aud: Decimal, entry_price: Decimal, stop_price: Decimal,
                  fx_to_aud: Decimal, instrument_type: str, adv_20d: int,
                  limits: Limits, breaker: BreakerLevel = BreakerLevel.NONE,
                  lot_size: int = 1) -> SizeDecision:
    """Deterministic §4 sizing. Size is an output of risk, never conviction."""
    if nav_aud <= 0 or entry_price <= 0 or fx_to_aud <= 0 or lot_size <= 0:
        raise ValueError("nav, entry price, fx and lot size must be positive")
    if stop_price >= entry_price or stop_price < 0:
        return SizeDecision(0, False, "stop", "stop must be below entry (long-only)")

    risk_budget = nav_aud * limits.risk_per_trade(breaker)
    per_share_risk_aud = (entry_price - stop_price) * fx_to_aud
    raw_size = risk_budget / per_share_risk_aud

    weight_cap_pct = (limits.l2_max_etf_weight if instrument_type == "etf"
                      else limits.l1_max_stock_weight)
    weight_cap = (weight_cap_pct * nav_aud) / (entry_price * fx_to_aud)
    liquidity_cap = limits.l10_max_pct_adv * Decimal(max(adv_20d, 0))

    candidates = {"L6": raw_size, "L1/L2": weight_cap, "L10": liquidity_cap}
    binding = min(candidates, key=lambda k: candidates[k])
    size = candidates[binding]

    lots = (size / lot_size).to_integral_value(rounding=ROUND_FLOOR)
    qty = int(lots) * lot_size
    if qty <= 0:
        return SizeDecision(0, False, binding, "size rounds to zero")
    value = Decimal(qty) * entry_price * fx_to_aud
    if value < MIN_POSITION_AUD:
        return SizeDecision(0, False, "min_position",
                            f"value {value:.2f} AUD < {MIN_POSITION_AUD} minimum")
    return SizeDecision(qty, True, binding,
                        f"qty {qty} bound by {binding}, value {value:.2f} AUD")
