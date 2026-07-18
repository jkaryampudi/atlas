"""Sector backfill on pick ingest: a non-S&P name comes in without a GICS
sector, so _backfill_sector resolves it from the fundamentals snapshot (the
same closed mapping backfill_gics uses) BEFORE the feature snapshot is taken —
never overwriting an existing sector.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from atlas.ops.ingest_picks import _backfill_sector
from tests.conftest import requires_pg

pytestmark = requires_pg


def _instrument(s, sym, sector=None):
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        " name, currency, is_active, sector_gics) "
        "VALUES (:s,'US','US','stock',:s,'USD',false,:sec) RETURNING id"),
        {"s": sym, "sec": sector}).scalar()


def _fundamentals(s, iid, payload):
    s.execute(text(
        "INSERT INTO market.fundamentals (instrument_id, as_of, payload, source) "
        "VALUES (:i, '2026-07-18', CAST(:p AS jsonb), 'test')"),
        {"i": iid, "p": json.dumps(payload)})


def test_backfill_sets_missing_sector_from_gicsector(pg_session):
    s = pg_session
    iid = _instrument(s, "ZALGM", sector=None)          # analysis-only, no sector
    _fundamentals(s, iid, {"General": {"GicSector": "Information Technology"}})
    _backfill_sector(s, iid)
    got = s.execute(text("SELECT sector_gics FROM market.instruments WHERE id=:i"),
                    {"i": iid}).scalar()
    assert got == "Information Technology"


def test_backfill_maps_alternate_sector_taxonomy(pg_session):
    s = pg_session
    iid = _instrument(s, "ZHIMX", sector=None)
    # no GicSector; the vendor's General.Sector taxonomy -> closed GICS mapping
    _fundamentals(s, iid, {"General": {"Sector": "Technology"}})
    _backfill_sector(s, iid)
    got = s.execute(text("SELECT sector_gics FROM market.instruments WHERE id=:i"),
                    {"i": iid}).scalar()
    assert got == "Information Technology"


def test_backfill_never_overwrites_an_existing_sector(pg_session):
    s = pg_session
    iid = _instrument(s, "ZORCL", sector="Information Technology")
    _fundamentals(s, iid, {"General": {"GicSector": "Energy"}})   # would-be clobber
    _backfill_sector(s, iid)
    got = s.execute(text("SELECT sector_gics FROM market.instruments WHERE id=:i"),
                    {"i": iid}).scalar()
    assert got == "Information Technology"                # unchanged


def test_backfill_no_fundamentals_is_a_noop(pg_session):
    s = pg_session
    iid = _instrument(s, "ZNONE", sector=None)
    _backfill_sector(s, iid)                              # no fundamentals row
    got = s.execute(text("SELECT sector_gics FROM market.instruments WHERE id=:i"),
                    {"i": iid}).scalar()
    assert got is None                                   # fail-safe, no guess
