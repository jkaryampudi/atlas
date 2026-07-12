"""Migration 0015 cycle: downgrade removes validation.index_membership AND the
validation schema, upgrade restores them; PK(index_code, ticker) and NOT NULL
flags are structural; the seal is verified AT THE DATABASE — atlas_agent_reader
must hold no privilege on the validation schema or table (same discipline as
fxlab/0014). Also exercises the snapshot replace + load roundtrip with nulls
kept verbatim."""
from __future__ import annotations

import os
import subprocess
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from atlas.dcp.market_data.index_membership import (
    MembershipRow,
    load_membership,
    replace_membership,
)
from tests.conftest import ROOT, URL, _ensure_test_db, requires_pg

pytestmark = requires_pg

FETCHED = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)


def _alembic(*args: str) -> None:
    env = {**os.environ, "ATLAS_DATABASE_URL": URL}
    r = subprocess.run(["alembic", *args], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, f"alembic {' '.join(args)} failed:\n{r.stderr}"


def _scalar(sql: str):
    engine = create_engine(URL)
    try:
        with engine.connect() as c:
            return c.execute(text(sql)).scalar()
    finally:
        engine.dispose()


def test_migration_0015_downgrade_upgrade_cycle():
    _ensure_test_db()
    try:
        assert _scalar("SELECT to_regclass('validation.index_membership')") is not None
        _alembic("downgrade", "0014")
        assert _scalar("SELECT to_regclass('validation.index_membership')") is None
        assert _scalar("SELECT count(*) FROM pg_namespace "
                       "WHERE nspname = 'validation'") == 0    # schema gone too
        _alembic("upgrade", "head")
        assert _scalar("SELECT to_regclass('validation.index_membership')") is not None
    finally:
        _alembic("upgrade", "head")     # never leave the test DB below head


def test_seal_no_agent_reader_privileges():
    """The validation plane is sealed: the reasoning plane's read role must
    have NOTHING on it — no schema usage, no table select."""
    _ensure_test_db()
    assert _scalar("SELECT has_schema_privilege('atlas_agent_reader', "
                   "'validation', 'USAGE')") is False
    assert _scalar("SELECT has_table_privilege('atlas_agent_reader', "
                   "'validation.index_membership', 'SELECT')") is False


def _row(ticker: str, start: date | None, end: date | None, *,
         active: bool = False, delisted: bool = False) -> MembershipRow:
    return MembershipRow(index_code="GSPC.INDX", ticker=ticker, name=ticker,
                         start_date=start, end_date=end, is_active_now=active,
                         is_delisted=delisted)


def test_replace_and_load_roundtrip_keeps_nulls(pg_session):
    s = pg_session
    s.execute(text("DELETE FROM validation.index_membership"))   # in-txn only
    rows = [_row("AAA", date(2000, 6, 5), None, active=True),
            _row("BBB", None, date(2015, 12, 29), delisted=True)]
    assert replace_membership(s, rows, fetched_at=FETCHED) == 2
    got = load_membership(s)
    assert [(r.ticker, r.start_date, r.end_date, r.is_active_now, r.is_delisted)
            for r in got] == [
        ("AAA", date(2000, 6, 5), None, True, False),
        ("BBB", None, date(2015, 12, 29), False, True)]

    # a re-fetch replaces the snapshot wholesale (no stale vendor rows survive)
    assert replace_membership(s, [_row("CCC", date(2020, 1, 2), None,
                                       active=True)], fetched_at=FETCHED) == 1
    assert [r.ticker for r in load_membership(s)] == ["CCC"]


def test_replace_refuses_empty_and_wrong_index(pg_session):
    with pytest.raises(ValueError, match="empty membership snapshot"):
        replace_membership(pg_session, [], fetched_at=FETCHED)
    with pytest.raises(ValueError, match="different index_code"):
        replace_membership(pg_session, [_row("AAA", date(2020, 1, 2), None)],
                           index_code="OEX.INDX", fetched_at=FETCHED)


def test_pk_refuses_duplicate_ticker(pg_session):
    s = pg_session
    s.execute(text("DELETE FROM validation.index_membership"))
    ins = text("INSERT INTO validation.index_membership "
               "(index_code, ticker, name, start_date, end_date, is_active_now, "
               " is_delisted, fetched_at) "
               "VALUES ('GSPC.INDX', 'AAA', 'x', NULL, NULL, TRUE, FALSE, :f)")
    s.execute(ins, {"f": FETCHED})
    with pytest.raises(IntegrityError):
        s.execute(ins, {"f": FETCHED})


def test_flags_not_nullable(pg_session):
    with pytest.raises(IntegrityError):
        pg_session.execute(text(
            "INSERT INTO validation.index_membership "
            "(index_code, ticker, name, start_date, end_date, is_active_now, "
            " is_delisted, fetched_at) "
            "VALUES ('GSPC.INDX', 'NNN', 'x', NULL, NULL, NULL, FALSE, :f)"),
            {"f": FETCHED})
