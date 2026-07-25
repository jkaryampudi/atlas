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
    IssuerDriftError, held_position_issuer_drifted, issuer_break_count,
    issuer_drifted, pin_issuer, populate_identities,
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


def test_identity_break_versions_not_overwrites(pg_session):
    """F-002: a ticker reassigned to a different issuer VERSIONS the identity
    (close the old era, open a new one) instead of overwriting — so the OLD era
    still resolves to the OLD issuer, and the break is a durable multi-row history.
    Fails against pre-fix code, which silently repointed the open row's ISIN over
    the old valid_from (mis-vouching the new issuer historically)."""
    s = pg_session
    iid = _instrument(s, "SPLICE")
    _fundamentals(s, iid, isin="US3333333333")
    _bar(s, iid, date(2015, 1, 2))
    populate_identities(s, known_from=KF)
    assert issuer_break_count(s, iid) == 0

    refresh_identity(s, iid, {"General": {"ISIN": "US4444444444"}}, known_from=KF)

    old = resolve_identity(s, iid, as_of=date(2016, 1, 1))
    now = resolve_identity(s, iid)
    assert old is not None and old.isin == "US3333333333"   # old era, old issuer
    assert now is not None and now.isin == "US4444444444"   # now, new issuer
    assert not same_issuer(old, now)
    assert issuer_break_count(s, iid) == 1
    n_open = s.execute(text("SELECT count(*) FROM market.instrument_identity "
                            "WHERE instrument_id=:i AND valid_to IS NULL"),
                       {"i": iid}).scalar()
    assert n_open == 1                                        # one OPEN row invariant


def test_held_position_issuer_drift_helper(pg_session):
    """F-002 capital-safety: a position opened in the OLD issuer's era is flagged
    once the ticker is reassigned; a same-issuer refresh is not; an unassessable
    pre-identity holding is not."""
    s = pg_session
    iid = _instrument(s, "HELD")
    _fundamentals(s, iid, isin="US5555555555")
    _bar(s, iid, date(2015, 1, 2))
    populate_identities(s, known_from=KF)
    opened = datetime(2016, 6, 1, tzinfo=timezone.utc)
    assert held_position_issuer_drifted(s, iid, opened) is False
    refresh_identity(s, iid, {"General": {"ISIN": "US5555555555"}}, known_from=KF)
    assert held_position_issuer_drifted(s, iid, opened) is False       # same issuer
    refresh_identity(s, iid, {"General": {"ISIN": "US6666666666"}}, known_from=KF)
    assert held_position_issuer_drifted(s, iid, opened) is True        # reassigned
    ghost = _instrument(s, "GHOST2", active=False)
    assert held_position_issuer_drifted(s, ghost, opened) is False     # unassessable


def test_reconciliation_breaks_on_held_position_issuer_drift(clean_audit):
    """F-002 wiring: an OPEN position whose ticker was reassigned to a different
    issuer since open makes the daily reconciliation BREAK (fail closed) — a book
    we can no longer trust is halted, not silently marked. Control: no drift ->
    clean. The lot-ledger leg is kept consistent so only the issuer-drift leg can
    break it."""
    from atlas.core.clock import FrozenClock
    from atlas.ops.daily import _reconcile
    s = clean_audit
    iid = _instrument(s, "RECON")
    _fundamentals(s, iid, isin="US7777777777")
    _bar(s, iid, date(2015, 1, 2))
    populate_identities(s, known_from=KF)
    opened = datetime(2016, 6, 1, tzinfo=timezone.utc)
    pid = s.execute(text(
        "INSERT INTO trading.positions (instrument_id, qty, avg_cost, currency, "
        " opened_at, current_stop, created_at) "
        "VALUES (:i, 10, 100, 'USD', :o, 90, :o) RETURNING id"),
        {"i": iid, "o": opened}).scalar()
    s.execute(text(
        "INSERT INTO trading.tax_lots (position_id, qty, cost_aud, acquired_at, "
        " created_at) VALUES (:p, 10, 1500, :a, :a)"), {"p": pid, "a": opened})
    clock = FrozenClock(KF)

    assert _reconcile(s, clock) == "clean"                     # no drift yet
    refresh_identity(s, iid, {"General": {"ISIN": "US8888888888"}}, known_from=KF)
    with pytest.raises(RuntimeError, match="reconciliation BREAK"):
        _reconcile(s, clock)                                   # reassigned -> halt


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
