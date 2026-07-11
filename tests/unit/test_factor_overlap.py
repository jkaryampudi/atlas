"""Factor-overlap guard (Doc 04 §12): pro-forma NAV-weighted market / sector /
momentum loadings against registered caps — two momentum names in one sector
are one bet, and the engine must see that even when every L1-L11 limit passes."""
from decimal import Decimal

import pytest

from atlas.dcp.risk.factor_overlap import (
    FactorCaps,
    FactorLoadings,
    check_factor_overlap,
)

NAV = Decimal("100000")


def _name(symbol="MSFT", value="5000", beta="1.0", sector="Information Technology",
          momentum="0") -> FactorLoadings:
    return FactorLoadings(symbol=symbol, value_aud=Decimal(value),
                          market_beta=Decimal(beta), sector_gics=sector,
                          momentum=Decimal(momentum))


def test_v1_seed_caps_are_documented_defaults():
    caps = FactorCaps()
    assert caps.market == Decimal("1.0")
    assert caps.sector == Decimal("0.25")
    assert caps.momentum == Decimal("0.5")


def test_clean_book_passes_with_itemised_detail():
    held = [_name("MSFT", "8000", "1.1", momentum="1.0"),
            _name("SPY", "20000", "1.0", sector="Broad")]
    r = check_factor_overlap(_name("JNJ", "6000", "0.6", "Health Care"),
                             held, nav_aud=NAV)
    # market: 0.08x1.1 + 0.20x1.0 + 0.06x0.6 = 0.324; momentum 0.08x1.0 = 0.08;
    # max sector IT 0.08 (Broad is not a sector bet)
    assert r.rule == "FACTOR" and r.passed
    assert "market 0.3240" in r.detail
    assert "momentum 0.0800" in r.detail
    assert "max sector Information Technology 0.0800" in r.detail


def test_two_momentum_names_in_one_sector_are_one_bet():
    """Doc 04 §12 verbatim scenario. NVDA (held) and AVGO (proposed) each pass
    every engine limit alone: 8% weight (L1 boundary), IT sector 16% (L3 25%),
    and each alone is under the momentum cap (0.08 x 3.5 = 0.28; 0.08 x 3.0 =
    0.24, both <= 0.5). Together the pro-forma momentum loading is
    0.28 + 0.24 = 0.52 > 0.5 — one bet, and the guard rejects it."""
    nvda = _name("NVDA", "8000", "1.6", momentum="3.5")
    avgo = _name("AVGO", "8000", "1.4", momentum="3.0")
    # each name alone passes the factor guard
    assert check_factor_overlap(nvda, [], nav_aud=NAV).passed
    assert check_factor_overlap(avgo, [], nav_aud=NAV).passed
    # together they are one momentum bet: rejected
    r = check_factor_overlap(avgo, [nvda], nav_aud=NAV)
    assert not r.passed
    assert "momentum 0.5200 > cap 0.5" in r.detail
    # market (0.24) and sector (0.16) stay within caps — momentum is the breach
    assert "market" not in r.detail.split("BREACH:")[1]
    assert "sector" not in r.detail.split("BREACH:")[1]


def test_market_loading_breach():
    # 0.40 x 1.8 (held) + 0.30 x 2.0 (proposal) = 0.72 + 0.60 = 1.32 > 1.0
    held = [_name("TQQQ-ish", "40000", "1.8", momentum="0")]
    r = check_factor_overlap(_name("HIBETA", "30000", "2.0", "Financials"),
                             held, nav_aud=NAV)
    assert not r.passed and "market 1.3200 > cap 1.0" in r.detail


def test_sector_loading_breach():
    # IT weight 0.15 held + 0.12 proposed = 0.27 > 0.25
    held = [_name("MSFT", "15000")]
    r = check_factor_overlap(_name("AVGO", "12000"), held, nav_aud=NAV)
    assert not r.passed
    assert "sector Information Technology 0.2700 > cap 0.25" in r.detail


def test_broad_etfs_are_not_a_sector_bet():
    # 40% in Broad ETFs would smash the 25% sector cap if Broad counted
    held = [_name("SPY", "25000", sector="Broad"),
            _name("INDA", "15000", sector="Broad")]
    r = check_factor_overlap(_name("VTI", "10000", sector="Broad"),
                             held, nav_aud=NAV)
    assert r.passed and "no sector exposure (Broad only)" in r.detail


def test_custom_caps_override_seeds():
    # the same clean single name fails when the registered momentum cap tightens
    p = _name("NVDA", "8000", momentum="3.5")  # loading 0.28
    assert check_factor_overlap(p, [], nav_aud=NAV).passed
    tight = FactorCaps(momentum=Decimal("0.25"))
    assert not check_factor_overlap(p, [], nav_aud=NAV, caps=tight).passed


def test_requires_positive_nav():
    with pytest.raises(ValueError, match="NAV"):
        check_factor_overlap(_name(), [], nav_aud=Decimal("0"))
