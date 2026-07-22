"""F-009 (pure): the cumulative split factor primitive and EPS re-basing.

Boundary convention MUST mirror adjust_for_splits exactly: a per-share quantity
dated strictly before a split's action_date is pre-split. So the factor over
(lo, hi] is product(ratio for lo < action_date <= hi) — lower STRICT, upper
INCLUSIVE — and EPS DIVIDES by a forward split (per-share shrinks)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from atlas.dcp.market_data.adjustment import (adjust_for_splits,
                                              cumulative_split_factor)
from atlas.dcp.market_data.earnings_basis import rebase_surprises
from atlas.dcp.market_data.earnings_history import EarningsSurprise
from atlas.dcp.market_data.models import Bar, Split


def _split(d: str, ratio) -> Split:
    return Split(symbol="X", action_date=date.fromisoformat(d), ratio=Decimal(ratio))


AAPL = [_split("2014-06-09", 7), _split("2020-08-31", 4)]  # real AAPL splits


def test_empty_and_out_of_range_factor_is_one():
    assert cumulative_split_factor([], date(2019, 1, 1), date(2021, 1, 1)) == 1
    # both splits outside (lo, hi]
    assert cumulative_split_factor(AAPL, date(2021, 1, 1), date(2022, 1, 1)) == 1
    # inverted interval (lo > hi) -> empty -> 1
    assert cumulative_split_factor(AAPL, date(2022, 1, 1), date(2019, 1, 1)) == 1


def test_compounding_and_bounds():
    # (2013, 2021] spans BOTH splits -> 7 * 4 = 28
    assert cumulative_split_factor(AAPL, date(2013, 1, 1), date(2021, 1, 1)) == 28
    # lower bound is STRICT: a split ON lo is already in the lo basis -> excluded
    assert cumulative_split_factor(AAPL, date(2020, 8, 31), date(2021, 1, 1)) == 1
    # upper bound is INCLUSIVE: a split ON hi is in force at hi -> included
    assert cumulative_split_factor(AAPL, date(2020, 1, 1), date(2020, 8, 31)) == 4
    # lo=None means -inf: every split with action_date <= hi
    assert cumulative_split_factor(AAPL, None, date(2021, 1, 1)) == 28


def test_parity_with_price_adjuster():
    """The factor that re-bases a quantity dated `d` to 'now' must equal the
    divisor adjust_for_splits applies to a price bar dated `d`."""
    d = date(2015, 1, 2)                         # after the 2014 split, before 2020
    bar = Bar(symbol="X", bar_date=d, open=Decimal(100), high=Decimal(100),
              low=Decimal(100), close=Decimal(100), volume=1000)
    adjusted = adjust_for_splits([bar], AAPL)[0]
    price_divisor = Decimal(100) / adjusted.close      # what the adjuster divided by
    factor = cumulative_split_factor(AAPL, d, date(2026, 1, 1))
    assert factor == price_divisor == 4                # only the 2020 4:1 applies


def _surprise(fpe: str, actual, estimate, basis: str) -> EarningsSurprise:
    return EarningsSurprise(
        symbol="X", fiscal_period_end=date.fromisoformat(fpe),
        report_date=date.fromisoformat(fpe), eps_actual=Decimal(actual),
        eps_estimate=Decimal(estimate), surprise_pct=None,
        before_after_market=None, split_basis_asof=date.fromisoformat(basis))


def test_rebase_divides_by_forward_split():
    # a row on the 2019 basis, read at 2021: the 2020 4:1 split halves-then-quarters
    r = _surprise("2019-12-31", "8.00", "7.60", basis="2019-06-01")
    out = rebase_surprises([r], AAPL, knowledge_date=date(2021, 1, 1))[0]
    assert out.eps_actual == Decimal(2)             # 8.00 / 4  (DIVIDES, not *)
    assert out.eps_estimate == Decimal("1.90")


def test_rebase_reconciles_mixed_basis_to_uniform():
    """The core F-009 property: two rows stored on DIFFERENT bases (a split
    between their fetches) reconcile to ONE common basis on read."""
    split = [_split("2025-08-15", 2)]
    old = _surprise("2025-03-31", "4.00", "3.80", basis="2025-08-01")  # pre-split fetch
    new = _surprise("2025-09-30", "2.10", "2.00", basis="2025-11-01")  # post-split fetch
    out = rebase_surprises([old, new], split, knowledge_date=date(2025, 12, 1))
    # old halves (split is after its basis); new unchanged (split before its basis)
    assert out[0].eps_actual == Decimal("2.00")     # 4.00 / 2
    assert out[1].eps_actual == Decimal("2.10")     # already post-split
    # both now on the post-split basis -> a mixed store became uniform


def test_rebase_is_noop_without_basis_or_splits():
    r = EarningsSurprise(symbol="X", fiscal_period_end=date(2020, 1, 1),
                         report_date=date(2020, 2, 1), eps_actual=Decimal(5),
                         eps_estimate=Decimal(4), surprise_pct=None,
                         before_after_market=None, split_basis_asof=None)
    assert rebase_surprises([r], AAPL, knowledge_date=date(2026, 1, 1))[0] is r
    r2 = _surprise("2025-01-01", "5", "4", basis="2025-01-01")
    assert rebase_surprises([r2], [], knowledge_date=date(2026, 1, 1))[0] is r2


def test_sue_invariant_no_phantom_from_basis_switch():
    """A signal built from re-based surprises must be identical whether or not a
    split+partial-refetch happened — the split is invisible after normalisation
    (SUE is invariant to a uniform basis)."""
    # scenario A: never split, single basis
    a = [_surprise("2025-03-31", "4.00", "3.80", "2025-08-01"),
         _surprise("2025-06-30", "4.20", "4.00", "2025-08-01"),
         _surprise("2025-09-30", "4.40", "4.10", "2025-08-01")]
    # scenario B: a 2:1 split, old rows frozen pre-split, new row post-split
    split = [_split("2025-08-15", 2)]
    b = [_surprise("2025-03-31", "4.00", "3.80", "2025-08-01"),
         _surprise("2025-06-30", "4.20", "4.00", "2025-08-01"),
         _surprise("2025-09-30", "2.20", "2.05", "2025-11-01")]   # post-split (halved)
    K = date(2025, 12, 1)
    na = rebase_surprises(a, split, knowledge_date=K)
    nb = rebase_surprises(b, split, knowledge_date=K)
    # the per-quarter surprise (actual-estimate) must match between A and B after
    # normalisation -> the split induced NO phantom surprise
    for x, y in zip(na, nb):
        assert (x.eps_actual - x.eps_estimate) == (y.eps_actual - y.eps_estimate)
