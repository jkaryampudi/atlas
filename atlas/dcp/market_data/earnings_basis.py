"""F-009 — re-base stored earnings surprises onto one common split basis.

market.earnings_surprises stores per-share EPS on the split basis current at each
row's fetch time (``split_basis_asof``, migration 0038). A re-ingest after a new
split leaves old-basis rows frozen (ON CONFLICT DO NOTHING) while new rows arrive
on the new basis — a single instrument's series can then MIX bases, and SUE
(a cross-quarter comparison) is invariant to a uniform basis but corrupts under a
mixed one.

This module reconciles a set of surprise rows onto ONE common basis at a
knowledge date K, look-ahead safely and immutably (no stored row is rewritten):

    eps_on_basis_K = eps_stored / cumulative_split_factor(splits, split_basis_asof, K)

using only splits with ``split_basis_asof < action_date <= K`` — the splits that
occurred AFTER the row's basis epoch and were therefore not yet baked in. EPS is
per-share, so it DIVIDES by a forward split exactly like a price (adjustment.py).
``surprise_pct`` is a split-neutral ratio and is left untouched.

Look-ahead is structural: callers load splits capped at K (``action_date <= K``)
and pass K as the upper bound, so a split effective after K is invisible — the
same ``action_date <= K`` rule used by the price/momentum/scorecard readers.

Pure functions + one capped loader; no agent imports (two-plane wall).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.dcp.market_data.adjustment import cumulative_split_factor
from atlas.dcp.market_data.earnings_history import EarningsSurprise
from atlas.dcp.market_data.models import Split


def rebase_surprises(reports: Sequence[EarningsSurprise],
                     splits: Sequence[Split], *,
                     knowledge_date: date) -> list[EarningsSurprise]:
    """Return ``reports`` with every per-share EPS re-based to the basis current
    at ``knowledge_date``. Splits must be pre-filtered to this instrument and
    capped at ``knowledge_date``. A row whose ``split_basis_asof`` is None or
    whose factor is 1 (no intervening split) is returned unchanged."""
    out: list[EarningsSurprise] = []
    for r in reports:
        if r.split_basis_asof is None:
            out.append(r)
            continue
        factor = cumulative_split_factor(splits, r.split_basis_asof, knowledge_date)
        if factor == 1:
            out.append(r)
            continue
        out.append(replace(
            r, eps_actual=r.eps_actual / factor,
            eps_estimate=r.eps_estimate / factor))
    return out


def load_splits_by_symbol(session: Session, symbols: Sequence[str], *,
                          up_to: date) -> dict[str, list[Split]]:
    """Split corporate actions per symbol, capped at ``up_to`` (look-ahead safe).
    One query; mirrors the capped split reads used across the codebase
    (xsmom/generate.py, scanner). Symbols with no splits are absent from the map
    (callers treat a missing key as an empty list)."""
    if not symbols:
        return {}
    rows = session.execute(text(
        "SELECT i.symbol AS symbol, ca.action_date AS action_date, "
        "       ca.ratio AS ratio "
        "FROM market.corporate_actions ca "
        "JOIN market.instruments i ON i.id = ca.instrument_id "
        "WHERE ca.action_type = 'split' AND ca.action_date <= :up_to "
        "  AND i.symbol = ANY(:syms) "
        "ORDER BY i.symbol, ca.action_date"),
        {"up_to": up_to, "syms": list(symbols)}).all()
    by_symbol: dict[str, list[Split]] = {}
    for r in rows:
        by_symbol.setdefault(str(r.symbol), []).append(
            Split(symbol=str(r.symbol), action_date=r.action_date,
                  ratio=Decimal(r.ratio)))
    return by_symbol


def load_instrument_splits(session: Session, instrument_id: object, *,
                           up_to: date) -> list[Split]:
    """Split corporate actions for one instrument_id, capped at ``up_to``."""
    rows = session.execute(text(
        "SELECT action_date, ratio FROM market.corporate_actions "
        "WHERE instrument_id = :iid AND action_type = 'split' "
        "  AND action_date <= :up_to ORDER BY action_date"),
        {"iid": instrument_id, "up_to": up_to}).all()
    return [Split(symbol=str(instrument_id), action_date=r.action_date,
                  ratio=Decimal(r.ratio)) for r in rows]


def rebase_reports_by_symbol(reports: Mapping[str, Sequence[EarningsSurprise]],
                             splits_by_symbol: Mapping[str, Sequence[Split]], *,
                             knowledge_date: date) -> dict[str, list[EarningsSurprise]]:
    """Re-base a per-symbol map of surprises (the shape pead/generate.py and
    pead_pit_run.py build) onto the common ``knowledge_date`` basis."""
    return {sym: rebase_surprises(rs, splits_by_symbol.get(sym, ()),
                                  knowledge_date=knowledge_date)
            for sym, rs in reports.items()}
