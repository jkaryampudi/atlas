"""ADR-0016 universe-activation mechanism — pure-logic pins (no DB).

These pins are load-bearing: L3 sector aggregation (risk engine), factor
overlap, and stress all match sectors on EXACT string equality, so the GICS
vocabulary the backfill may write is pinned verbatim to the 11 values already
in use in market.instruments. The vendor field mapping (General.GicSector
preferred, General.Sector translated as fallback) was probed live 2026-07-18
on A.US / ABNB.US / ADM.US and cross-checked against all 115 stored
market.fundamentals payloads. Constants (sanity band, freshness rule) are
documented in their modules; changing one is a reviewed change, so a change
must break a test here first.
"""
from __future__ import annotations

from datetime import date

from atlas.dcp.market_data import index_membership, universe
from atlas.tools import activate_universe as au
from atlas.tools import backfill_gics as bg

# The 11 official GICS sectors, exactly as existing market.instruments rows
# spell them (probed on dev 2026-07-18). "Broad" (diversified ETFs) is NOT a
# stock sector and must never be a backfill result.
GICS_11 = {
    "Communication Services", "Consumer Discretionary", "Consumer Staples",
    "Energy", "Financials", "Health Care", "Industrials",
    "Information Technology", "Materials", "Real Estate", "Utilities",
}


def test_gics_vocabulary_is_the_exact_11_sector_set():
    assert bg.GICS_SECTORS == frozenset(GICS_11)
    assert "Broad" not in bg.GICS_SECTORS


def test_vendor_fallback_mapping_lands_inside_the_vocabulary():
    assert set(bg.VENDOR_SECTOR_TO_GICS.values()) <= bg.GICS_SECTORS
    # the vendor's alternate General.Sector taxonomy, exactly as probed
    assert bg.VENDOR_SECTOR_TO_GICS["Technology"] == "Information Technology"
    assert bg.VENDOR_SECTOR_TO_GICS["Healthcare"] == "Health Care"
    assert bg.VENDOR_SECTOR_TO_GICS["Financial Services"] == "Financials"
    assert bg.VENDOR_SECTOR_TO_GICS["Consumer Cyclical"] == "Consumer Discretionary"
    assert bg.VENDOR_SECTOR_TO_GICS["Consumer Defensive"] == "Consumer Staples"
    assert bg.VENDOR_SECTOR_TO_GICS["Basic Materials"] == "Materials"
    for identity in ("Communication Services", "Energy", "Industrials",
                     "Real Estate", "Utilities"):
        assert bg.VENDOR_SECTOR_TO_GICS[identity] == identity


def test_resolve_sector_prefers_gicsector_verbatim():
    assert bg.resolve_sector({"General": {"GicSector": "Health Care",
                                          "Sector": "Technology"}}) == "Health Care"


def test_resolve_sector_falls_back_to_mapped_vendor_sector():
    assert (bg.resolve_sector({"General": {"Sector": "Consumer Cyclical"}})
            == "Consumer Discretionary")
    assert (bg.resolve_sector({"General": {"GicSector": "",
                                           "Sector": "Healthcare"}}) == "Health Care")
    # an out-of-vocabulary GicSector must not block a valid fallback
    assert (bg.resolve_sector({"General": {"GicSector": "Junk",
                                           "Sector": "Technology"}})
            == "Information Technology")


def test_resolve_sector_fails_closed_outside_both_vocabularies():
    assert bg.resolve_sector({"General": {"GicSector": "Junk",
                                          "Sector": "Widgets"}}) is None
    assert bg.resolve_sector({"General": {}}) is None
    assert bg.resolve_sector({}) is None
    assert bg.resolve_sector({"General": "not-a-dict"}) is None
    # hostile non-string values in the slots are not sectors
    assert bg.resolve_sector({"General": {"GicSector": 42, "Sector": None}}) is None


def test_freshness_threshold_counts_xnys_sessions():
    assert au.freshness_threshold(date(2026, 7, 10), 0) == date(2026, 7, 10)
    assert au.freshness_threshold(date(2026, 7, 10), 1) == date(2026, 7, 9)
    # weekend skip: 2026-07-11/12 are Sat/Sun
    assert au.freshness_threshold(date(2026, 7, 13), 1) == date(2026, 7, 10)


def test_documented_constants_pinned():
    assert (au.SANITY_MIN, au.SANITY_MAX) == (350, 420)
    assert au.MAX_STALE_SESSIONS == 10
    assert au.RECONCILE_MAX_CHANGES == 40
    # single source of truth: freshness is anchored on the PIT backfill end
    assert au.FRESH_REF == index_membership.PRICE_END == date(2026, 7, 10)
    assert au.INDEX_CODE == bg.INDEX_CODE == "GSPC.INDX"


def test_manifest_entry_shape_matches_sync_universe_contract():
    # activation extends seeds/universe.json with entries sync_universe accepts
    assert au.MANIFEST_FIELDS == universe.REQUIRED_FIELDS
