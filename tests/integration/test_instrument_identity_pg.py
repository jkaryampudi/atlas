"""F-002: instrument / issuer identity resolution.

Proves the identity layer on synthetic data mirroring the reviewed contamination:
a permanent-identifier model populated from real fundamentals, fail-closed on
unresolved / ambiguous / before-series-floor lookups, drift detection under a
held position, and the F-001 reused-ticker corroboration. No fabricated ids."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text

from atlas.dcp.market_data.identity import (
    IssuerDriftError, issuer_drifted, pin_issuer, populate_identities,
    refresh_identity, require_stable_issuer, resolve_by_symbol, resolve_identity,
    reused_ticker_is_identity_unvouched, same_issuer)
from tests.conftest import requires_pg

pytestmark = requires_pg

KF = datetime(2026, 7, 22, tzinfo=timezone.utc)


def _instrument(s, symbol, *, name="X", active=True, exchange="US") -> str:
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type,"
        " name, currency, is_active) VALUES (:sym,:exch,'US','stock',:nm,'USD',:act)"
        " RETURNING id"),
        {"sym": symbol, "exch": exchange, "nm": name, "act": active}).scalar())


def _fundamentals(s, iid, *, isin=None, cusip=None, prim=None, ccy="USD",
                  country="US", as_of=date(2026, 7, 1)) -> None:
    general = {"CurrencyCode": ccy, "CountryISO": country}
    if isin is not None:
        general["ISIN"] = isin
    if cusip is not None:
        general["CUSIP"] = cusip
    if prim is not None:
        general["PrimaryTicker"] = prim
    import json
    s.execute(text(
        "INSERT INTO market.fundamentals (instrument_id, as_of, payload, source) "
        "VALUES (:iid,:d, CAST(:p AS jsonb),'test')"),
        {"iid": iid, "d": as_of, "p": json.dumps({"General": general})})


def _bar(s, iid, d, px=100.0) -> None:
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high, low,"
        " close, volume, source) VALUES (:i,:d,:p,:p,:p,:p,1,'test')"),
        {"i": iid, "d": d, "p": px})


# --------------------------------------------------------------------------- #
def test_resolves_isin_and_pins_first_bar_as_valid_from(pg_session):
    s = pg_session
    iid = _instrument(s, "AAA")
    _fundamentals(s, iid, isin="US0000000001", cusip="000000001", prim="AAA.US")
    _bar(s, iid, date(2015, 3, 2))
    _bar(s, iid, date(2015, 3, 3))
    out = populate_identities(s, known_from=KF)
    assert out.resolved == 1 and out.unresolved == 0
    idn = resolve_by_symbol(s, "AAA")
    assert idn is not None
    assert idn.isin == "US0000000001"
    assert idn.valid_from == date(2015, 3, 2)   # first stored bar, the vouchable floor
    assert idn.history_complete is False         # honest: single snapshot, no change-history
    assert idn.identity_currency == "USD"


def test_no_permanent_identifier_fails_closed(pg_session):
    s = pg_session
    iid = _instrument(s, "NOID", active=False)
    _fundamentals(s, iid, isin="", cusip="")     # has fundamentals, no ISIN/CUSIP
    _bar(s, iid, date(2016, 1, 4))
    out = populate_identities(s, known_from=KF)
    assert out.unresolved == 1 and out.resolved == 0
    assert resolve_by_symbol(s, "NOID") is None  # unresolved -> None


def test_no_fundamentals_yields_no_identity(pg_session):
    s = pg_session
    iid = _instrument(s, "NOFUND")
    _bar(s, iid, date(2016, 1, 4))               # bars but no fundamentals row
    populate_identities(s, known_from=KF)
    # no identity row at all -> resolve fails closed
    assert resolve_identity(s, iid) is None


def test_cusip_only_resolves_via_cusip_key(pg_session):
    s = pg_session
    iid = _instrument(s, "CUS")
    _fundamentals(s, iid, isin=None, cusip="123456789")
    _bar(s, iid, date(2017, 6, 1))
    populate_identities(s, known_from=KF)
    idn = resolve_by_symbol(s, "CUS")
    assert idn is not None and idn.isin is None and idn.cusip == "123456789"


def test_reused_ticker_before_series_fails_closed(pg_session):
    """ADT-shape: the current issuer's series starts long after the stale S&P era.
    resolve as-of the era must fail closed (we do NOT claim the current identity
    applied then); as-of a date inside the series resolves."""
    s = pg_session
    iid = _instrument(s, "ADTX")
    _fundamentals(s, iid, isin="US00090Q1031")
    _bar(s, iid, date(2018, 1, 19))              # current issuer IPO'd 2018
    _bar(s, iid, date(2019, 1, 2))
    populate_identities(s, known_from=KF)
    assert resolve_identity(s, iid, as_of=date(2014, 1, 1)) is None    # stale era
    assert resolve_identity(s, iid, as_of=date(2019, 1, 2)) is not None
    # F-001 corroboration: the era is identity-unvouched
    assert reused_ticker_is_identity_unvouched(s, "ADTX", as_of=date(2014, 1, 1)) is True


def test_same_issuer_and_fail_closed_on_unknown(pg_session):
    s = pg_session
    a = _instrument(s, "TWNA")
    b = _instrument(s, "TWNB")
    c = _instrument(s, "OTHER")
    _fundamentals(s, a, isin="US1111111111")
    _bar(s, a, date(2015, 1, 2))
    _fundamentals(s, b, isin="US1111111111")     # same ISIN as a
    _bar(s, b, date(2015, 1, 2))
    _fundamentals(s, c, isin="US2222222222")
    _bar(s, c, date(2015, 1, 2))
    populate_identities(s, known_from=KF)
    ia, ib, ic = (resolve_by_symbol(s, x) for x in ("TWNA", "TWNB", "OTHER"))
    assert same_issuer(ia, ib) is True           # shared ISIN
    assert same_issuer(ia, ic) is False          # different ISIN
    assert same_issuer(ia, None) is False         # unknown fails closed


def test_issuer_drift_under_held_position(pg_session):
    """A ticker reassigned to a different issuer under a held position: the pinned
    key no longer resolves -> drift detected, and require_stable_issuer raises."""
    s = pg_session
    iid = _instrument(s, "REUSE")
    _fundamentals(s, iid, isin="US3333333333")
    _bar(s, iid, date(2015, 1, 2))
    populate_identities(s, known_from=KF)
    pinned = pin_issuer(s, iid)
    assert pinned == "ISIN:US3333333333"
    assert issuer_drifted(s, iid, pinned) is False
    # vendor now serves a DIFFERENT issuer for the same instrument row
    refresh_identity(s, iid, {"General": {"ISIN": "US4444444444"}}, known_from=KF)
    assert issuer_drifted(s, iid, pinned) is True
    with pytest.raises(IssuerDriftError):
        require_stable_issuer(s, iid, pinned)


def test_pin_refuses_unresolved_instrument(pg_session):
    s = pg_session
    iid = _instrument(s, "GHOST", active=False)
    _fundamentals(s, iid, isin="")               # unresolved
    populate_identities(s, known_from=KF)
    with pytest.raises(IssuerDriftError):
        pin_issuer(s, iid)


def test_ambiguous_symbol_fails_closed(pg_session):
    s = pg_session
    a = _instrument(s, "DUP", exchange="US")
    b = _instrument(s, "DUP", exchange="AU")     # same symbol, two instruments
    _fundamentals(s, a, isin="US5555555555")
    _bar(s, a, date(2015, 1, 2))
    _fundamentals(s, b, isin="AU6666666666")
    _bar(s, b, date(2015, 1, 2))
    populate_identities(s, known_from=KF)
    assert resolve_by_symbol(s, "DUP") is None   # ambiguity -> fail closed


def test_populate_idempotent_single_open_row(pg_session):
    s = pg_session
    iid = _instrument(s, "IDEM")
    _fundamentals(s, iid, isin="US7777777777")
    _bar(s, iid, date(2015, 1, 2))
    populate_identities(s, known_from=KF)
    out2 = populate_identities(s, known_from=KF)   # re-run
    assert out2.resolved == 1
    n_open = s.execute(text(
        "SELECT count(*) FROM market.instrument_identity "
        "WHERE instrument_id=:i AND valid_to IS NULL"), {"i": iid}).scalar()
    assert n_open == 1                            # exactly one OPEN identity
