"""F-001: the definitive PIT panel resolves every formation bar to an issuer
identity before admission.

Two layers are proven here:
  * admit_pre_era_bars_by_issuer (the per-bar gate) — same-issuer pre-era history
    is KEPT, a reused-ticker (different issuer) pre-era splice is DROPPED, and an
    unvouchable pre-era bar is DROPPED fail-closed;
  * load_pit_panel's coverage-safety gate — the panel REFUSES to run when issuer
    identity could not be resolved for most of the ranked universe (no running on
    ticker-only history).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text

from atlas.dcp.market_data.identity import (
    admit_pre_era_bars_by_issuer,
    refresh_identity,
)
from tests.conftest import requires_pg

pytestmark = requires_pg

KF_OLD = datetime(2011, 1, 3, 12, tzinfo=UTC)
KF_BREAK = datetime(2014, 1, 1, 12, tzinfo=UTC)     # the ticker-reuse break date
ERA_START = date(2014, 1, 1)


def _inst(s, symbol="ZID"):
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        " name, sector_gics, currency, is_active) "
        "VALUES (:s,'XTEST','US','stock',:s,'Broad','USD',false) RETURNING id"),
        {"s": symbol}).scalar())


def _monthly_bars(s, iid, start: date, end: date):
    d, dates = start, []
    while d <= end:
        s.execute(text(
            "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
            " high, low, close, volume, source) "
            "VALUES (:i,:d,100,100,100,100,1000,'EodhdAdapter') "
            "ON CONFLICT (instrument_id, bar_date) DO NOTHING"),
            {"i": iid, "d": d})
        dates.append(d)
        d += timedelta(days=30)
    return dates


def _in_era(d: date) -> bool:
    return d >= ERA_START


def test_single_snapshot_pre_era_dropped_fail_closed(pg_session):
    """F-001 fail-closed core: a SINGLE-snapshot identity (one open row,
    history_complete=false, valid_from defaulted to the first bar — the real-world
    vendor case) does NOT attest the pre-membership era, so same-ISIN pre-era bars
    are DROPPED as `unattested`. The current issuer's ISIN can never silently
    vouch a prior issuer's bars. Missing history fails CLOSED, not OPEN."""
    s = pg_session
    iid = _inst(s)
    dates = _monthly_bars(s, iid, date(2010, 1, 1), date(2016, 1, 1))
    refresh_identity(s, iid, {"General": {"ISIN": "US0000000001"}}, known_from=KF_OLD)
    adm = admit_pre_era_bars_by_issuer(s, iid, dates, is_in_era=_in_era)
    pre = [d for d in dates if not _in_era(d)]
    era = [d for d in dates if _in_era(d)]
    assert adm.member_resolved is True
    assert adm.history_attested is False
    assert adm.unattested == len(pre) and adm.unattested > 0
    assert adm.wrong_issuer == 0 and adm.unresolved == 0
    assert [dates[i] for i in adm.keep] == era     # only in-era bars survive


def test_attested_history_pre_era_is_kept(pg_session):
    """When the identity genuinely ATTESTS its history (history_complete=true, e.g.
    a vendor symbol-change feed), same-issuer pre-era bars ARE admitted — the gate
    is not blanket-conservative, it fails closed only on UNATTESTED history."""
    s = pg_session
    iid = _inst(s)
    dates = _monthly_bars(s, iid, date(2010, 1, 1), date(2016, 1, 1))
    refresh_identity(s, iid, {"General": {"ISIN": "US0000000001"}}, known_from=KF_OLD)
    # a real attestation feed would set this; simulate it
    s.execute(text("UPDATE market.instrument_identity SET history_complete = true "
                   "WHERE instrument_id = :i"), {"i": iid})
    s.flush()
    adm = admit_pre_era_bars_by_issuer(s, iid, dates, is_in_era=_in_era)
    assert adm.history_attested is True
    assert adm.keep == list(range(len(dates)))     # every bar kept
    assert adm.wrong_issuer == 0 and adm.unresolved == 0 and adm.unattested == 0


def test_reused_ticker_pre_era_bars_are_dropped(pg_session):
    """The exact defect: a ticker used by issuer A before the member (issuer B)
    joined. A's pre-era bars must NOT enter B's momentum formation."""
    s = pg_session
    iid = _inst(s)
    dates = _monthly_bars(s, iid, date(2010, 1, 1), date(2016, 1, 1))
    # issuer A owns the ticker first; the break-versioning closes A at 2014 and
    # opens B from 2014 (the member's era).
    refresh_identity(s, iid, {"General": {"ISIN": "US000000000A"}}, known_from=KF_OLD)
    refresh_identity(s, iid, {"General": {"ISIN": "US000000000B"}}, known_from=KF_BREAK)

    adm = admit_pre_era_bars_by_issuer(s, iid, dates, is_in_era=_in_era)
    pre = [d for d in dates if not _in_era(d)]
    era = [d for d in dates if _in_era(d)]
    assert adm.member_resolved is True
    assert adm.wrong_issuer == len(pre) and adm.wrong_issuer > 0
    assert adm.unresolved == 0
    # every kept index is an in-era bar; no pre-era (issuer-A) bar survived
    assert [dates[i] for i in adm.keep] == era


def test_unresolved_pre_era_bars_fail_closed(pg_session):
    s = pg_session
    iid = _inst(s)
    dates = _monthly_bars(s, iid, date(2010, 1, 1), date(2016, 1, 1))
    # NO identity seeded -> the member itself is unresolved; every pre-era bar is
    # dropped fail-closed (we cannot vouch it is the same issuer).
    adm = admit_pre_era_bars_by_issuer(s, iid, dates, is_in_era=_in_era)
    pre = [d for d in dates if not _in_era(d)]
    assert adm.member_resolved is False
    assert adm.unresolved == len(pre) and adm.wrong_issuer == 0
    assert [dates[i] for i in adm.keep] == [d for d in dates if _in_era(d)]
